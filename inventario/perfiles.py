"""
Traduce el inventario de un cliente al esquema canonico de inventario.py.

Cada cliente entrega el suyo con otras columnas, otras unidades y otro
vocabulario. En vez de adivinar, cada uno tiene un perfil declarativo en
`perfiles/<cliente>.yml` que dice como leerlo. El perfil se guarda junto a la
propuesta: es lo que permite rehacer el mismo calculo un ano despues.

Una fila del cliente puede producir VARIAS filas canonicas -- lo normal es que
una linea de VM traiga tambien el tamano de su disco.
"""

import csv
import fnmatch
import io
import os

import yaml

AQUI = os.path.dirname(os.path.abspath(__file__))

# A bytes. GB/TB son decimales (10^n); GiB/TiB binarias (2^n). Los inventarios
# los mezclan sin avisar y la diferencia en TB es del 10%.
UNIDADES = {
    "B": 1,
    "KB": 10**3, "KIB": 2**10,
    "MB": 10**6, "MIB": 2**20,
    "GB": 10**9, "GIB": 2**30,
    "TB": 10**12, "TIB": 2**40,
    "PB": 10**15, "PIB": 2**50,
}

CAMPOS = ["tipo", "nombre", "region", "spec", "cantidad", "unidad", "so",
          "compromiso", "padre", "notas"]


class ErrorPerfil(Exception):
    pass


# --------------------------------------------------------------------------
# Tipos de maquina, para dimensionar desde vCPU + RAM
# --------------------------------------------------------------------------

# Familias que el sizing considera por defecto: proposito general y optimizadas
# para computo. Las demas (GPU: a2/a3/a4/g2/g4 - memoria: m1..m4 - HPC: h3) caben
# por vCPU y RAM pero cuestan un orden de magnitud mas, y elegirlas por accidente
# es un error caro. Para usarlas hay que pedirlas por 'familia' en el perfil.
FAMILIAS_GENERALES = ("e2", "n1", "n2", "n2d", "n4", "t2a", "t2d",
                      "c2", "c2d", "c3", "c3d", "c4", "c4a", "c4d")


def cargar_maquinas(ruta=None):
    """tools/machinetypes.csv -> [(nombre, cpus, ram_gb)], sin obsoletas."""
    ruta = ruta or os.path.join(AQUI, "..", "tools", "machinetypes.csv")
    if not os.path.exists(ruta):
        raise ErrorPerfil(
            f"no encuentro {ruta}, que es de donde salen los tipos de maquina.\n"
            f"  Sin el no se puede dimensionar desde vCPU y RAM; indica el tipo "
            f"de maquina con 'desde' o 'constante'.")
    maquinas = []
    with open(ruta, newline="", encoding="utf-8") as fh:
        for fila in csv.DictReader(fh, delimiter=";"):
            if (fila.get("DEPRECATED") or "").strip():
                continue
            if (fila.get("SHARED_CPU") or "").strip().lower() == "true":
                continue  # e2-micro y companyia: no sirven para una carga real
            try:
                maquinas.append((fila["NAME"], int(fila["CPUS"]),
                                 float(fila["MEMORY_GB"])))
            except (KeyError, ValueError):
                continue
    if not maquinas:
        raise ErrorPerfil(f"{ruta}: no se pudo leer ningun tipo de maquina")
    return maquinas


def cargar_precios(region, ruta=None):
    """pricing.yml -> {tipo: USD/mes on-demand} en una region.

    El mismo fichero que usa gcosts, asi que el tipo elegido y el precio con el
    que luego se cobra salen de la misma fuente. Un tipo que no aparezca aqui no
    existe en esa region: no se puede proponer.
    """
    ruta = ruta or os.path.join(AQUI, "..", "pricing.yml")
    if not os.path.exists(ruta):
        return {}
    try:
        from yaml import CSafeLoader as Cargador
    except ImportError:
        from yaml import SafeLoader as Cargador
    with open(ruta, encoding="utf-8") as fh:
        datos = yaml.load(fh, Loader=Cargador)
    precios = {}
    for tipo, info in (datos.get("compute", {}).get("instance", {}) or {}).items():
        coste = ((info.get("cost") or {}).get(region) or {}).get("month")
        if coste is not None:
            precios[tipo] = float(coste)
    return precios


def dimensionar(vcpu, ram_gib, maquinas, familia=None, precios=None):
    """El tipo mas pequeno que cabe. Nunca uno que se quede corto.

    ram_gib va en GiB, que es la unidad de MEMORY_GB en machinetypes.csv pese al
    nombre de la columna: n2-standard-8 aparece como 32 y son 32 GiB.

    Sin 'familia' solo se consideran las familias de FAMILIAS_GENERALES. Ordenar
    unicamente por (vCPU, RAM) haria que una maquina con GPU de 12 vCPU ganase a
    una estandar de 16, que es mas barata.

    Entre las que caben se elige la MAS BARATA segun pricing.yml, no la primera
    por orden alfabetico: c2-standard-8 y n2-standard-8 tienen los mismos 8 vCPU
    y 32 GiB, y se llevan 17 USD/mes de diferencia.
    """
    aptas = [m for m in maquinas if m[1] >= vcpu and m[2] >= ram_gib]
    if familia:
        prefijos = tuple(f"{f.strip()}-" for f in familia.split("|"))
        etiqueta = f" de {familia}"
    else:
        prefijos = tuple(f"{f}-" for f in FAMILIAS_GENERALES)
        etiqueta = " de proposito general"
    aptas = [m for m in aptas if m[0].startswith(prefijos)]
    if precios:
        # Un tipo sin precio en la region no existe alli: no se puede proponer.
        aptas = [m for m in aptas if m[0] in precios]

    if not aptas:
        raise ErrorPerfil(
            f"no hay tipo de maquina{etiqueta} que llegue a {vcpu} vCPU y "
            f"{ram_gib} GiB de RAM.\n"
            f"  Si la carga necesita una familia especial (GPU, memoria, HPC), "
            f"declarala con 'familia' en el perfil.")
    if precios:
        aptas.sort(key=lambda m: (precios[m[0]], m[1], m[2], m[0]))
    else:
        # Sin pricing.yml: a igualdad de recursos, el nombre mas corto.
        aptas.sort(key=lambda m: (m[1], m[2], len(m[0]), m[0]))
    return aptas[0][0]


# --------------------------------------------------------------------------
# Perfil
# --------------------------------------------------------------------------

def cargar_perfil(ruta):
    with open(ruta, encoding="utf-8") as fh:
        perfil = yaml.safe_load(fh)
    if not isinstance(perfil, dict):
        raise ErrorPerfil(f"{ruta}: no es un YAML de perfil")
    if "salidas" not in perfil:
        raise ErrorPerfil(
            f"{ruta}: falta 'salidas'. Es la lista de filas canonicas que produce "
            f"cada fila del cliente.")
    if not isinstance(perfil["salidas"], list) or not perfil["salidas"]:
        raise ErrorPerfil(f"{ruta}: 'salidas' tiene que ser una lista no vacia")
    for i, salida in enumerate(perfil["salidas"], 1):
        if "tipo" not in salida:
            raise ErrorPerfil(f"{ruta}: la salida {i} no declara 'tipo'")
        for campo in salida:
            if campo not in CAMPOS and campo != "omitir_si_vacio":
                raise ErrorPerfil(
                    f"{ruta}: la salida {i} declara '{campo}', que no es un campo "
                    f"canonico.\n  Validos: {', '.join(CAMPOS)}, omitir_si_vacio")
    return perfil


def _traducir(valor, mapa):
    """Primer patron que case, en el orden escrito. '*' al final es el cajon."""
    for patron, destino in mapa.items():
        if fnmatch.fnmatch(valor.lower(), str(patron).lower()):
            return "" if destino is None else str(destino)
    raise ErrorPerfil(
        f"el valor {valor!r} no casa con ningun patron del mapa.\n"
        f"  Patrones: {', '.join(repr(p) for p in mapa)}\n"
        f"  Anade '*': <valor> al final si quieres un cajon de sastre.")


def _convertir(bruto, unidad, decimal=".") -> float:
    if isinstance(unidad, dict):
        de, a = unidad.get("de", "GiB"), unidad.get("a", "GiB")
    else:
        de, a = unidad, "GiB"
    de, a = str(de).upper(), str(a).upper()
    for u in (de, a):
        if u not in UNIDADES:
            raise ErrorPerfil(
                f"unidad '{u}' desconocida. Validas: {', '.join(sorted(UNIDADES))}")
    texto = str(bruto).strip()
    if decimal == ",":
        texto = texto.replace(".", "").replace(",", ".")
    try:
        numero = float(texto)
    except ValueError:
        raise ErrorPerfil(f"{bruto!r} no es un numero")
    return round(numero * UNIDADES[de] / UNIDADES[a], 4)


def _resolver(spec, fila, ctx, campo):
    """Un campo del perfil -> su valor para esta fila del cliente."""
    if spec is None:
        return ""
    if not isinstance(spec, dict):
        return str(spec)  # atajo: 'region: us-central1'

    if "sizing" in spec:
        s = spec["sizing"]
        for clave in ("vcpu", "ram_gb"):
            if clave not in s:
                raise ErrorPerfil(f"campo '{campo}': sizing necesita '{clave}'")
        try:
            vcpu = float(str(fila[s["vcpu"]]).replace(",", "."))
            ram = float(str(fila[s["ram_gb"]]).replace(",", "."))
        except KeyError as e:
            raise ErrorPerfil(f"campo '{campo}': el CSV no tiene la columna {e}")
        except ValueError:
            raise ErrorPerfil(
                f"campo '{campo}': vCPU o RAM no son numeros "
                f"({fila.get(s['vcpu'])!r}, {fila.get(s['ram_gb'])!r})")
        if s.get("ram_unidad"):
            ram = _convertir(ram, {"de": s["ram_unidad"], "a": "GiB"})
        region = s.get("region") or ctx.get("region") or "us-central1"
        if region not in ctx.setdefault("precios", {}):
            ctx["precios"][region] = cargar_precios(region)
        return dimensionar(vcpu, ram, ctx["maquinas"], s.get("familia"),
                           ctx["precios"][region])

    if "constante" in spec:
        valor = str(spec["constante"])
    elif "desde" in spec:
        col = spec["desde"]
        if col not in fila:
            raise ErrorPerfil(
                f"campo '{campo}': el CSV no tiene la columna '{col}'.\n"
                f"  Columnas: {', '.join(fila.keys())}")
        valor = (fila[col] or "").strip()
    else:
        raise ErrorPerfil(
            f"campo '{campo}': hay que decir de donde sale "
            f"(constante, desde o sizing)")

    if valor and "mapa" in spec:
        valor = _traducir(valor, spec["mapa"])
    if valor and "unidad" in spec:
        valor = str(_convertir(valor, spec["unidad"], ctx.get("decimal", ".")))
    if valor:
        valor = f"{spec.get('prefijo', '')}{valor}{spec.get('sufijo', '')}"
    return valor


def _pasa_filtros(fila, filtros):
    for f in filtros or []:
        col = f.get("columna")
        if col not in fila:
            raise ErrorPerfil(f"filtro sobre '{col}', que no es una columna del CSV")
        valor = (fila[col] or "").strip().lower()
        excluir = [str(v).lower() for v in f.get("excluir", [])]
        incluir = [str(v).lower() for v in f.get("incluir", [])]
        if excluir and any(fnmatch.fnmatch(valor, p) for p in excluir):
            return False
        if incluir and not any(fnmatch.fnmatch(valor, p) for p in incluir):
            return False
    return True


def leer_csv_cliente(ruta, perfil):
    conf = perfil.get("csv", {}) or {}
    delim = conf.get("delimitador", ",")
    cod = conf.get("codificacion", "utf-8-sig")
    saltar = int(conf.get("saltar_lineas", 0))
    with io.open(ruta, encoding=cod, newline="", errors="replace") as fh:
        for _ in range(saltar):
            fh.readline()
        return list(csv.DictReader(fh, delimiter=delim))


def normalizar(ruta_csv, perfil, region=None):
    """CSV del cliente -> filas canonicas. Devuelve (filas, avisos)."""
    ctx = {"decimal": (perfil.get("csv", {}) or {}).get("decimal", "."),
           "region": region}
    necesita_sizing = any("sizing" in (s.get("spec") or {})
                          for s in perfil["salidas"]
                          if isinstance(s.get("spec"), dict))
    ctx["maquinas"] = cargar_maquinas() if necesita_sizing else []

    origen = leer_csv_cliente(ruta_csv, perfil)
    if not origen:
        raise ErrorPerfil(f"{ruta_csv}: no tiene ninguna fila")

    filas, avisos, descartadas = [], [], 0
    for n, cruda in enumerate(origen, start=2):
        cruda = {(k or "").strip(): (v or "").strip() for k, v in cruda.items() if k}
        if not any(cruda.values()):
            continue
        if not _pasa_filtros(cruda, perfil.get("filtrar")):
            descartadas += 1
            continue
        for i, salida in enumerate(perfil["salidas"], 1):
            try:
                fila = {c: _resolver(salida.get(c), cruda, ctx, c) for c in CAMPOS}
            except ErrorPerfil as e:
                raise ErrorPerfil(f"{ruta_csv}:{n}, salida {i}: {e}")
            omitir = salida.get("omitir_si_vacio")
            if omitir and not fila.get(omitir):
                continue
            if not fila.get("nombre"):
                raise ErrorPerfil(
                    f"{ruta_csv}:{n}, salida {i}: la fila sale sin nombre")
            filas.append(fila)

    if descartadas:
        avisos.append(f"{descartadas} fila(s) descartadas por los filtros del perfil")
    if not filas:
        raise ErrorPerfil(
            f"{ruta_csv}: el perfil no produjo ninguna fila. "
            f"Revisa 'filtrar' y los 'omitir_si_vacio'.")
    return filas, avisos


# --------------------------------------------------------------------------
# Inspeccion: que trae este CSV y como podria mapearse
# --------------------------------------------------------------------------

PISTAS = {
    "nombre": ["name", "nombre", "hostname", "host", "vm", "servidor", "server",
               "instancia", "instance", "recurso"],
    "vcpu": ["cpu", "cpus", "vcpu", "vcpus", "cores", "nucleos", "procesadores"],
    "ram": ["ram", "mem", "memory", "memoria"],
    "disco": ["disk", "disco", "storage", "almacenamiento", "capacity",
              "capacidad", "provisioned"],
    "so": ["os", "so", "sistema", "operating", "guest", "plataforma"],
    "region": ["region", "zone", "zona", "location", "ubicacion", "site", "dc"],
    "tipo": ["type", "tipo", "clase", "class", "kind", "categoria", "servicio"],
    "estado": ["state", "estado", "status", "power"],
}


def _pista(cabecera):
    c = cabecera.lower()
    return [rol for rol, claves in PISTAS.items() if any(k in c for k in claves)]


def detectar_delimitador(ruta, codificacion="utf-8-sig"):
    with io.open(ruta, encoding=codificacion, errors="replace") as fh:
        muestra = fh.readline()
    return max([",", ";", "\t", "|"], key=muestra.count)


def inspeccionar(ruta, delimitador=None, muestras=3):
    delim = delimitador or detectar_delimitador(ruta)
    with io.open(ruta, encoding="utf-8-sig", newline="", errors="replace") as fh:
        filas = list(csv.DictReader(fh, delimiter=delim))
    if not filas:
        raise ErrorPerfil(f"{ruta}: no tiene filas de datos")

    columnas = []
    for cab in (filas[0].keys()):
        if cab is None:
            continue
        valores = [(f.get(cab) or "").strip() for f in filas]
        no_vacios = [v for v in valores if v]
        numerico = bool(no_vacios) and all(
            v.replace(",", ".").replace(" ", "").replace(".", "", 1).isdigit()
            for v in no_vacios)
        columnas.append({
            "cabecera": cab.strip(),
            "distintos": len(set(no_vacios)),
            "vacios": len(valores) - len(no_vacios),
            "numerica": numerico,
            "muestra": list(dict.fromkeys(no_vacios))[:muestras],
            "pistas": _pista(cab),
        })
    return {"delimitador": delim, "filas": len(filas), "columnas": columnas}
