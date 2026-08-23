#!/usr/bin/env python3
"""
inventario -- de un CSV de inventario a un CSV de costes mensuales de GCP.

Dos motores de precio, porque ninguno cubre todo:

  gcosts   maquinas, discos, buckets, VPN, NAT, monitoring, egress.
           Precios derivados y validados con 543 asertos contra la calculadora
           oficial. Se ejecuta como Cloud Run Job.

  tarifas  todo lo demas (BigQuery, Logging, Pub/Sub, Bigtable, Dataplex...).
           Precio unitario leido de la Cloud Billing Catalog API y anclado en
           un tarifas.yml con fecha, para que una propuesta se pueda reproducir.

El reparto lo decide el campo `tipo` de cada fila. Los tipos de gcosts estan en
TIPOS_GCOSTS; el resto se buscan en servicios.csv.
"""

import argparse
import csv
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

import yaml

import perfiles

AQUI = os.path.dirname(os.path.abspath(__file__))
HORAS_MES = 730  # el mismo valor que usa gcosts para pasar de hora a mes

# Factor por el que se multiplica la cantidad para llevarla a coste mensual.
# Un periodo desconocido NO cae a 1: aborta. Un tarifas.yml viejo con nombres de
# periodo antiguos daria un coste 730 veces menor sin que nada lo delate.
FACTOR_PERIODO = {"mes": 1, "consumo": 1, "hora-permanente": HORAS_MES}

# Tipos que resuelve gcosts. El valor es la seccion del YAML de uso que generan.
TIPOS_GCOSTS = {
    "vm": "instances",
    "disco": "disks",
    "bucket": "buckets",
    "egress": "traffic",
    "vpn": "vpn-tunnels",
    "nat": "nat-gateways",
    "monitoring": "monitoring",
}

COLUMNAS = ["tipo", "nombre", "region", "spec", "cantidad", "unidad", "so",
            "compromiso", "padre", "notas"]

# gcosts solo cobra licencia de estos SO. Un SO sin licencia (Debian, Ubuntu,
# CentOS...) no lleva campo 'os': ponerle uno inventado aborta el calculo entero.
SO_CON_LICENCIA = {"rhel", "rhel-sap", "sles", "sles-sap", "windows"}
SO_SIN_LICENCIA = {"", "-", "free", "libre", "none", "ninguno", "debian",
                   "ubuntu", "centos", "rocky", "linux"}


class ErrorInventario(Exception):
    """Error atribuible al fichero de entrada o al mapeo, no al programa."""


# --------------------------------------------------------------------------
# Lectura del inventario
# --------------------------------------------------------------------------

def _filas_csv(ruta):
    """Lee un CSV ignorando lineas de comentario y en blanco."""
    with open(ruta, newline="", encoding="utf-8") as fh:
        utiles = (l for l in fh if l.strip() and not l.lstrip().startswith("#"))
        for n, fila in enumerate(csv.DictReader(utiles), start=2):
            yield n, {(k or "").strip(): (v or "").strip()
                      for k, v in fila.items() if k}


def leer_inventario(ruta):
    filas = []
    for n, fila in _filas_csv(ruta):
        desconocidas = set(fila) - set(COLUMNAS)
        if desconocidas:
            raise ErrorInventario(
                f"{ruta}:{n}: columnas no reconocidas: {', '.join(sorted(desconocidas))}\n"
                f"  columnas validas: {', '.join(COLUMNAS)}")
        for obligatoria in ("tipo", "nombre"):
            if not fila.get(obligatoria):
                raise ErrorInventario(f"{ruta}:{n}: falta '{obligatoria}'")
        fila["_linea"] = n
        filas.append(fila)
    if not filas:
        raise ErrorInventario(f"{ruta}: no hay ninguna fila de inventario")
    # Dos recursos del mismo tipo con el mismo nombre son indistinguibles en el
    # CSV de costes, y un disco que apunte a ese nombre no sabe de que vm
    # cuelga. No es un aviso: el resultado seria un numero creible y ambiguo.
    vistos = {}
    for fila in filas:
        clave = (fila["tipo"], fila["nombre"])
        if clave in vistos:
            raise ErrorInventario(
                f"{ruta}:{fila['_linea']}: nombre duplicado "
                f"'{fila['nombre']}' para el tipo '{fila['tipo']}' "
                f"(ya estaba en la linea {vistos[clave]}).\n"
                f"  Cada recurso necesita un nombre unico: es lo que une un "
                f"disco con su vm y lo que identifica la linea en la propuesta.")
        vistos[clave] = fila["_linea"]
    return filas


def leer_servicios(ruta=None):
    ruta = ruta or os.path.join(AQUI, "servicios.csv")
    servicios = {}
    for n, fila in _filas_csv(ruta):
        tipo = fila["tipo"]
        if tipo in TIPOS_GCOSTS:
            raise ErrorInventario(
                f"{ruta}:{n}: '{tipo}' ya lo calcula gcosts; no puede estar aqui")
        if tipo in servicios:
            raise ErrorInventario(f"{ruta}:{n}: tipo duplicado '{tipo}'")
        if fila["periodo"] not in ("mes", "hora-permanente", "consumo"):
            raise ErrorInventario(
                f"{ruta}:{n}: periodo '{fila['periodo']}' no valido "
                f"(mes|hora-permanente|consumo)")
        servicios[tipo] = fila
    return servicios


def _num(fila, campo="cantidad"):
    bruto = fila.get(campo, "")
    if not bruto:
        raise ErrorInventario(
            f"linea {fila['_linea']} ({fila['nombre']}): falta '{campo}'")
    try:
        return float(bruto.replace(",", "."))
    except ValueError:
        raise ErrorInventario(
            f"linea {fila['_linea']} ({fila['nombre']}): "
            f"'{campo}' no es un numero: {bruto!r}")


# --------------------------------------------------------------------------
# Catalogo de tarifas (Cloud Billing Catalog API)
# --------------------------------------------------------------------------

def _token():
    r = subprocess.run(["gcloud", "auth", "print-access-token"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise ErrorInventario(
            "no hay credenciales de gcloud.\n"
            "  Ejecuta: gcloud auth login  (o gcloud auth application-default login)")
    return r.stdout.strip()


def _skus(servicio_id, token):
    """Todos los SKUs de un servicio, paginando."""
    salida, page = [], None
    while True:
        url = (f"https://cloudbilling.googleapis.com/v1/services/{servicio_id}"
               f"/skus?pageSize=500" + (f"&pageToken={page}" if page else ""))
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
        try:
            datos = json.load(urllib.request.urlopen(req))
        except urllib.error.HTTPError as e:
            raise ErrorInventario(
                f"la Billing API rechazo la consulta de {servicio_id}: "
                f"{e.code} {e.reason}\n  {e.read()[:300].decode('utf-8', 'replace')}")
        salida += datos.get("skus", [])
        page = datos.get("nextPageToken")
        if not page:
            return salida


def _precio(sku):
    """Precio del ultimo tramo. Google expresa el importe en units + nanos."""
    expr = sku["pricingInfo"][0]["pricingExpression"]
    tarifa = expr["tieredRates"][-1]["unitPrice"]
    importe = int(tarifa.get("units", 0)) + tarifa.get("nanos", 0) / 1e9
    return importe, tarifa["currencyCode"], expr["usageUnitDescription"]


def resolver(servicio, region, skus):
    """Un patron -> exactamente un SKU. 0 o >1 es error, nunca se elige uno."""
    patron = servicio["sku_patron"].replace("{region}", re.escape(region))
    rx = re.compile(patron, re.I)

    candidatos = [s for s in skus
                  if region in s.get("serviceRegions", [])
                  or "global" in s.get("serviceRegions", [])]
    casan = [s for s in candidatos if rx.search(s["description"])]

    if not casan:
        raise ErrorInventario(
            f"tipo '{servicio['tipo']}' en {region}: ningun SKU casa con "
            f"/{servicio['sku_patron']}/\n"
            f"  {len(candidatos)} SKUs disponibles en esa region. "
            f"Revisa servicios.csv o si el servicio existe ahi.")
    if len(casan) > 1:
        muestra = "\n".join(f"    - {s['description']}" for s in casan[:6])
        raise ErrorInventario(
            f"tipo '{servicio['tipo']}' en {region}: {len(casan)} SKUs casan con "
            f"/{servicio['sku_patron']}/, y el precio seria ambiguo:\n{muestra}\n"
            f"  Afina el patron en servicios.csv.")

    sku = casan[0]
    importe, moneda, unidad = _precio(sku)
    esperada = servicio.get("unidad_esperada", "").strip()
    if esperada and unidad != esperada:
        raise ErrorInventario(
            f"tipo '{servicio['tipo']}' en {region}: la API factura en '{unidad}' "
            f"pero servicios.csv espera '{esperada}'.\n"
            f"  Google cambio la unidad del SKU: revisa el calculo antes de seguir.")
    return {
        "sku_id": sku["skuId"],
        "descripcion": sku["description"],
        "precio": importe,
        "moneda": moneda,
        "unidad": unidad,
        "periodo": servicio["periodo"],
        "servicio": servicio["servicio"],
    }


def generar_tarifas(regiones, tipos=None, ruta_servicios=None):
    servicios = leer_servicios(ruta_servicios)
    if tipos:
        faltan = set(tipos) - set(servicios)
        if faltan:
            raise ErrorInventario(
                f"tipos no definidos en servicios.csv: {', '.join(sorted(faltan))}")
        servicios = {t: servicios[t] for t in tipos}

    token = _token()
    cache, tarifas, errores = {}, {}, []
    for tipo, servicio in servicios.items():
        sid = servicio["servicio_id"]
        if sid not in cache:
            cache[sid] = _skus(sid, token)
        for region in regiones:
            try:
                tarifas.setdefault(region, {})[tipo] = resolver(
                    servicio, region, cache[sid])
            except ErrorInventario as e:
                errores.append(str(e))
    return {
        "about": {
            "generado": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
            "fuente": "Cloud Billing Catalog API (cloudbilling.googleapis.com/v1)",
            "regiones": list(regiones),
            "aviso": ("Precios de lista publicos. No incluye acuerdos empresariales "
                      "ni descuentos negociados."),
        },
        "tarifas": tarifas,
    }, errores


# --------------------------------------------------------------------------
# Reparto del inventario entre los dos motores
# --------------------------------------------------------------------------

def repartir(filas, servicios):
    gcosts, tarifadas, desconocidas = [], [], []
    for fila in filas:
        tipo = fila["tipo"]
        if tipo in TIPOS_GCOSTS:
            gcosts.append(fila)
        elif tipo in servicios:
            tarifadas.append(fila)
        else:
            desconocidas.append(fila)
    return gcosts, tarifadas, desconocidas


# --------------------------------------------------------------------------
# Motor 1: generacion del YAML de uso para gcosts
# --------------------------------------------------------------------------

def _disco(fila):
    d = {"name": fila["nombre"], "type": fila["spec"] or "balanced",
         "data": _num(fila)}
    if fila.get("region"):
        d["region"] = fila["region"]
    return d


def construir_yaml(filas, region_defecto, proyecto=None):
    """Un solo documento YAML con todo el inventario que gcosts sabe preciar."""
    doc = {"region": region_defecto}
    if proyecto:
        doc["project"] = proyecto

    discos_sueltos = [f for f in filas if f["tipo"] == "disco" and not f.get("padre")]
    por_padre = {}
    for f in filas:
        if f["tipo"] == "disco" and f.get("padre"):
            por_padre.setdefault(f["padre"], []).append(f)

    nombres_vm = {f["nombre"] for f in filas if f["tipo"] == "vm"}
    huerfanos = set(por_padre) - nombres_vm
    if huerfanos:
        raise ErrorInventario(
            "estos discos apuntan a una VM que no esta en el inventario: "
            + ", ".join(sorted(huerfanos)))

    for fila in filas:
        tipo, seccion = fila["tipo"], TIPOS_GCOSTS[fila["tipo"]]

        if tipo == "vm":
            if not fila.get("spec"):
                raise ErrorInventario(
                    f"linea {fila['_linea']} ({fila['nombre']}): una vm necesita "
                    f"'spec' con el tipo de maquina (p.ej. n2-standard-8)")
            item = {"name": fila["nombre"], "type": fila["spec"]}
            so = (fila.get("so") or "").lower()
            if so in SO_CON_LICENCIA:
                item["os"] = so
            elif so not in SO_SIN_LICENCIA:
                raise ErrorInventario(
                    f"linea {fila['_linea']} ({fila['nombre']}): so '{fila['so']}' "
                    f"no lo reconoce gcosts.\n"
                    f"  Con licencia: {', '.join(sorted(SO_CON_LICENCIA))}\n"
                    f"  Sin licencia (Debian, Ubuntu...): deja la columna vacia.")
            if fila.get("compromiso") and fila["compromiso"] != "0":
                item["commitment"] = int(fila["compromiso"])
            if fila.get("region"):
                item["region"] = fila["region"]
            adjuntos = por_padre.get(fila["nombre"], [])
            if adjuntos:
                item["disks"] = [_disco(d) for d in adjuntos]

        elif tipo == "disco":
            if fila in discos_sueltos:
                item = _disco(fila)
            else:
                continue  # ya colgado de su VM

        elif tipo == "bucket":
            item = {"name": fila["nombre"], "class": fila["spec"] or "standard",
                    "data": _num(fila)}
            if fila.get("region"):
                item["region"] = fila["region"]

        elif tipo == "egress":
            item = {"name": fila["nombre"], "world": _num(fila)}

        elif tipo == "vpn":
            item = {"name": fila["nombre"]}
            if fila.get("region"):
                item["region"] = fila["region"]

        elif tipo in ("nat", "monitoring"):
            item = {"name": fila["nombre"], "data": _num(fila)}
            if fila.get("region"):
                item["region"] = fila["region"]

        doc.setdefault(seccion, []).append(item)

    return doc


# --------------------------------------------------------------------------
# Motor 2: coste de las filas tarifadas
# --------------------------------------------------------------------------

def calcular_tarifadas(filas, tarifas, region_defecto, args_tarifas=None):
    resultados, errores = [], []
    for fila in filas:
        region = fila.get("region") or region_defecto
        tarifa = tarifas.get("tarifas", {}).get(region, {}).get(fila["tipo"])
        if not tarifa:
            errores.append(
                f"linea {fila['_linea']} ({fila['nombre']}): no hay tarifa de "
                f"'{fila['tipo']}' en {region}.\n"
                f"  Regenera con: inventario.py tarifas --regiones {region}")
            continue
        try:
            cantidad = _num(fila)
        except ErrorInventario as e:
            errores.append(str(e))
            continue

        periodo = tarifa.get("periodo")
        if periodo not in FACTOR_PERIODO:
            errores.append(
                f"linea {fila['_linea']} ({fila['nombre']}): la tarifa de "
                f"'{fila['tipo']}' declara periodo '{periodo}', que este programa "
                f"no conoce.\n"
                f"  Conocidos: {', '.join(sorted(FACTOR_PERIODO))}\n"
                f"  Suele significar que {os.path.basename(args_tarifas or 'tarifas.yml')} "
                f"es de una version anterior: regeneralo con 'inventario.py tarifas'.")
            continue
        factor = FACTOR_PERIODO[periodo]
        resultados.append({
            "region": region,
            "categoria": tarifa["servicio"],
            "nombre": fila["nombre"],
            "tipo": fila["tipo"],
            "cantidad": cantidad,
            "unidad": tarifa["unidad"],
            "precio_unitario": tarifa["precio"],
            "factor_mes": factor,
            "coste_mes": round(cantidad * tarifa["precio"] * factor, 2),
            "sku": tarifa["sku_id"],
            "motor": "tarifas",
        })
    return resultados, errores


# --------------------------------------------------------------------------
# Lectura del CSV que devuelve gcosts
# --------------------------------------------------------------------------

def leer_costes_gcosts(ruta):
    """Lee el costs.csv de gcosts.

    Cabecera real:
      Project,Region,Resource,Type/Class,Name,Cost,Data,CUD,Discount,File

    Se valida antes de leer: si gcosts cambia de formato, un nombre de columna
    equivocado daria un total de 0,00 sin avisar, y eso acabaria en una propuesta.
    """
    ESPERADAS = {"Region", "Resource", "Type/Class", "Name", "Cost", "Data"}
    salida = []
    with open(ruta, newline="", encoding="utf-8") as fh:
        lector = csv.DictReader(fh)
        faltan = ESPERADAS - set(lector.fieldnames or [])
        if faltan:
            raise ErrorInventario(
                f"{ruta}: no parece un costs.csv de gcosts.\n"
                f"  Faltan las columnas: {', '.join(sorted(faltan))}\n"
                f"  Cabecera encontrada: {', '.join(lector.fieldnames or ['(vacia)'])}")
        for n, fila in enumerate(lector, start=2):
            try:
                coste = float(fila.get("Cost") or 0)
            except ValueError:
                raise ErrorInventario(
                    f"{ruta}:{n}: 'Cost' no es un numero: {fila.get('Cost')!r}")
            salida.append({
                "region": fila.get("Region", ""),
                "categoria": fila.get("Resource", ""),
                "nombre": fila.get("Name", ""),
                "tipo": fila.get("Type/Class", ""),
                "cantidad": fila.get("Data", ""),
                "unidad": "",
                "precio_unitario": "",
                "factor_mes": "",
                "coste_mes": round(coste, 2),
                "sku": "",
                "motor": "gcosts",
            })
    if not salida:
        raise ErrorInventario(f"{ruta}: no tiene ninguna linea de coste")
    return salida


CABECERAS = ["motor", "categoria", "region", "nombre", "tipo", "cantidad",
             "unidad", "precio_unitario", "factor_mes", "coste_mes", "sku"]


def escribir_costes(filas, ruta):
    with open(ruta, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=CABECERAS)
        w.writeheader()
        for f in sorted(filas, key=lambda x: (x["motor"], x["categoria"], x["nombre"])):
            w.writerow({k: f.get(k, "") for k in CABECERAS})


# --------------------------------------------------------------------------
# Subcomandos
# --------------------------------------------------------------------------

def cmd_tarifas(args):
    regiones = [r.strip() for r in args.regiones.split(",") if r.strip()]
    catalogo, errores = generar_tarifas(regiones, args.tipos.split(",") if args.tipos else None)
    if errores:
        for e in errores:
            print(f"ERROR: {e}", file=sys.stderr)
        if not args.parcial:
            print(f"\n{len(errores)} tarifa(s) sin resolver. Nada escrito.\n"
                  f"Usa --parcial para escribir de todas formas.", file=sys.stderr)
            return 1
    with open(args.salida, "w", encoding="utf-8") as fh:
        yaml.safe_dump(catalogo, fh, allow_unicode=True, sort_keys=False)
    total = sum(len(v) for v in catalogo["tarifas"].values())
    print(f"{args.salida}: {total} tarifas en {len(catalogo['tarifas'])} region(es), "
          f"generadas {catalogo['about']['generado']}")
    return 0


def cmd_convertir(args):
    filas = leer_inventario(args.csv)
    servicios = leer_servicios()
    gcosts, tarifadas, desconocidas = repartir(filas, servicios)
    if desconocidas:
        validos = sorted(list(TIPOS_GCOSTS) + list(servicios))
        for f in desconocidas:
            print(f"ERROR: linea {f['_linea']} ({f['nombre']}): tipo "
                  f"'{f['tipo']}' desconocido", file=sys.stderr)
        print(f"\nTipos validos:\n  {', '.join(validos)}", file=sys.stderr)
        return 1

    os.makedirs(args.dir, exist_ok=True)
    if gcosts:
        doc = construir_yaml(gcosts, args.region, args.proyecto)
        destino = os.path.join(args.dir, "01-inventario.yml")
        with open(destino, "w", encoding="utf-8") as fh:
            yaml.safe_dump(doc, fh, allow_unicode=True, sort_keys=False)
        print(f"{destino}: {len(gcosts)} recurso(s) para gcosts")
    print(f"{len(tarifadas)} recurso(s) se preciaran con tarifas.yml")
    return 0


def cmd_calcular(args):
    filas = leer_inventario(args.csv)
    servicios = leer_servicios()
    gcosts_filas, tarifadas, desconocidas = repartir(filas, servicios)
    if desconocidas:
        validos = sorted(list(TIPOS_GCOSTS) + list(servicios))
        for f in desconocidas:
            print(f"ERROR: linea {f['_linea']} ({f['nombre']}): tipo "
                  f"'{f['tipo']}' desconocido", file=sys.stderr)
        print(f"\nTipos validos:\n  {', '.join(validos)}", file=sys.stderr)
        return 1

    spot = [f for f in filas if (f.get("notas") or "").lower().find("spot") >= 0]
    if spot:
        print("AVISO: hay filas que mencionan spot en 'notas'. El precio spot no es "
              "comprometible y no debe ir en una propuesta.", file=sys.stderr)

    resultados = []

    if tarifadas:
        if not os.path.exists(args.tarifas):
            print(f"ERROR: no existe {args.tarifas}. Generalo con:\n"
                  f"  {sys.argv[0]} tarifas --regiones {args.region}", file=sys.stderr)
            return 1
        with open(args.tarifas, encoding="utf-8") as fh:
            tarifas = yaml.safe_load(fh)
        print(f"tarifas.yml generado el {tarifas['about']['generado']}")
        parciales, errores = calcular_tarifadas(tarifadas, tarifas, args.region,
                                               args.tarifas)
        if errores:
            for e in errores:
                print(f"ERROR: {e}", file=sys.stderr)
            return 1
        resultados += parciales

    if gcosts_filas:
        if not args.costes_gcosts:
            print("ERROR: el inventario tiene recursos que precia gcosts, pero no se "
                  "indico --costes-gcosts con su costs.csv.\n"
                  "  Genera el YAML con 'convertir', ejecuta el Cloud Run Job y "
                  "pasa aqui el CSV resultante.\n"
                  "  Ver inventario/README.md, seccion 'Flujo completo'.", file=sys.stderr)
            return 1
        resultados += leer_costes_gcosts(args.costes_gcosts)

    escribir_costes(resultados, args.salida)
    total = sum(r["coste_mes"] for r in resultados)
    por_motor = {}
    for r in resultados:
        por_motor[r["motor"]] = por_motor.get(r["motor"], 0) + r["coste_mes"]
    print(f"\n{args.salida}: {len(resultados)} linea(s)")
    for motor, importe in sorted(por_motor.items()):
        print(f"  {motor:8} {importe:12,.2f} USD/mes")
    print(f"  {'TOTAL':8} {total:12,.2f} USD/mes")
    print("\nPrecios de lista. Sin acuerdos empresariales ni descuentos negociados.")
    return 0


def cmd_inspeccionar(args):
    info = perfiles.inspeccionar(args.csv, args.delimitador)
    print(f"{args.csv}: {info['filas']} filas, delimitador {info['delimitador']!r}\n")
    print(f"{'COLUMNA':32} {'DISTINTOS':>9} {'VACIOS':>7}  MUESTRA / PISTA")
    print("-" * 100)
    for c in info["columnas"]:
        muestra = " | ".join(x[:22] for x in c["muestra"]) or "(vacia)"
        pista = f"  <- podria ser {'/'.join(c['pistas'])}" if c["pistas"] else ""
        print(f"{c['cabecera'][:32]:32} {c['distintos']:>9} {c['vacios']:>7}  "
              f"{muestra[:40]}{pista}")

    if args.borrador:
        with open(args.borrador, "w", encoding="utf-8") as fh:
            fh.write(_borrador(args.csv, info))
        print(f"\nBorrador de perfil en {args.borrador}")
        print("NO lo uses sin revisarlo: las columnas son una conjetura a partir "
              "del nombre de la cabecera.")
    return 0


def _borrador(ruta_csv, info):
    """Perfil de partida. Acierta con suerte; siempre hay que repasarlo."""
    def busca(rol):
        for c in info["columnas"]:
            if rol in c["pistas"]:
                return c["cabecera"]
        return None

    nombre, vcpu = busca("nombre"), busca("vcpu")
    ram, disco, so = busca("ram"), busca("disco"), busca("so")
    lineas = [
        f"# Perfil generado a partir de {os.path.basename(ruta_csv)}.",
        "# REVISAR: las columnas son una conjetura por el nombre de la cabecera.",
        "#",
        f"nombre: {os.path.splitext(os.path.basename(ruta_csv))[0]}",
        "descripcion: |",
        "  De donde salio este inventario, quien lo entrego y cuando.",
        "",
        "csv:",
        f"  delimitador: {info['delimitador']!r}",
        "  decimal: '.'          # ',' si el cliente usa coma decimal",
        "",
        "# filtrar:",
        "#   - columna: Estado",
        "#     excluir: [retirado, decommissioned]",
        "",
        "salidas:",
        "  - tipo: {constante: vm}",
        f"    nombre: {{desde: {nombre!r}}}" if nombre
        else "    nombre: {desde: 'REVISAR-columna-del-nombre'}",
        "    region: {constante: us-central1}",
    ]
    if vcpu and ram:
        lineas += [
            "    # Dimensiona al tipo mas pequeno que cabe. Nunca se queda corto.",
            "    spec:",
            "      sizing:",
            f"        vcpu: {vcpu!r}",
            f"        ram_gb: {ram!r}",
            "        # ram_unidad: MB     # si la RAM viene en MB",
            "        # familia: n2|n2d    # para restringir la familia",
        ]
    else:
        lineas += ["    spec: {desde: 'REVISAR-tipo-de-maquina'}"]
    if so:
        lineas += [
            "    # 'so' es la LICENCIA que se cobra, no el sistema operativo.",
            "    # Solo rhel, rhel-sap, sles, sles-sap, windows. El resto: vacio.",
            "    so:",
            f"      desde: {so!r}",
            "      mapa:",
            "        '*red hat*': rhel",
            "        '*rhel*': rhel",
            "        '*suse*': sles",
            "        '*windows*': windows",
            "        '*': ''",
        ]
    if disco:
        lineas += [
            "",
            "  - tipo: {constante: disco}",
            f"    nombre: {{desde: {nombre!r}, sufijo: '-disco'}}" if nombre
            else "    nombre: {desde: 'REVISAR', sufijo: '-disco'}",
            "    spec: {constante: balanced}",
            f"    cantidad: {{desde: {disco!r}, unidad: GB}}",
            f"    padre: {{desde: {nombre!r}}}" if nombre
            else "    padre: {desde: 'REVISAR'}",
            "    omitir_si_vacio: cantidad",
        ]
    return "\n".join(lineas) + "\n"


def cmd_normalizar(args):
    perfil = perfiles.cargar_perfil(args.perfil)
    filas, avisos = perfiles.normalizar(args.csv, perfil, args.region)
    for a in avisos:
        print(f"aviso: {a}", file=sys.stderr)
    with open(args.salida, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=COLUMNAS)
        w.writeheader()
        for f in filas:
            w.writerow(f)
    reparto = {}
    for f in filas:
        reparto[f["tipo"]] = reparto.get(f["tipo"], 0) + 1
    print(f"{args.salida}: {len(filas)} fila(s) canonicas")
    for tipo, n in sorted(reparto.items()):
        print(f"  {tipo:34} {n:>5}")
    return 0


def main():
    p = argparse.ArgumentParser(
        description="De un CSV de inventario a un CSV de costes mensuales de GCP.")
    sub = p.add_subparsers(dest="cmd", required=True)

    t = sub.add_parser("tarifas", help="regenera el catalogo de tarifas desde la Billing API")
    t.add_argument("--regiones", required=True, help="lista separada por comas")
    t.add_argument("--tipos", default="", help="solo estos tipos (por defecto, todos)")
    t.add_argument("-o", "--salida", default="tarifas.yml")
    t.add_argument("--parcial", action="store_true",
                   help="escribe aunque alguna tarifa no resuelva")
    t.set_defaults(func=cmd_tarifas)

    c = sub.add_parser("convertir", help="inventario.csv -> YAML de uso para gcosts")
    c.add_argument("--csv", required=True)
    c.add_argument("--dir", required=True, help="directorio destino del YAML")
    c.add_argument("--region", required=True, help="region por defecto")
    c.add_argument("--proyecto", default=None)
    c.set_defaults(func=cmd_convertir)

    k = sub.add_parser("calcular", help="une ambos motores en un CSV de costes")
    k.add_argument("--csv", required=True)
    k.add_argument("--region", required=True)
    k.add_argument("--tarifas", default="tarifas.yml")
    k.add_argument("--costes-gcosts", default=None,
                   help="costs.csv devuelto por el Cloud Run Job")
    k.add_argument("--salida", default="costes.csv")
    k.set_defaults(func=cmd_calcular)

    i = sub.add_parser("inspeccionar",
                       help="mira el CSV de un cliente y propone un perfil")
    i.add_argument("--csv", required=True)
    i.add_argument("--delimitador", default=None, help="por defecto se detecta")
    i.add_argument("--borrador", default=None, help="escribe un perfil de partida")
    i.set_defaults(func=cmd_inspeccionar)

    n = sub.add_parser("normalizar",
                       help="CSV del cliente + perfil -> inventario canonico")
    n.add_argument("--csv", required=True)
    n.add_argument("--perfil", required=True)
    n.add_argument("--region", default="us-central1",
                   help="region para elegir el tipo de maquina mas barato que cabe")
    n.add_argument("--salida", default="inventario.csv")
    n.set_defaults(func=cmd_normalizar)

    args = p.parse_args()
    try:
        return args.func(args)
    except (ErrorInventario, perfiles.ErrorPerfil) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
