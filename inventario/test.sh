#!/usr/bin/env bash
#
# Pruebas de inventario.py. No tocan la red: usan las tarifas fijas de t/.
#
#   bash test.sh
#
# Comprueban dos cosas distintas:
#   - que los importes salen bien
#   - que los guardarrailes ABORTAN. Un calculo que se degrada en silencio es
#     peor que uno que falla, porque acaba en una propuesta.

set -u

AQUI="$(cd "$(dirname "$0")" && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

INV="python3 $AQUI/inventario.py"
OK=0
FALLOS=0

ok()   { printf '\033[32m✅ OK\033[0m    : %s\n' "$1"; OK=$((OK+1)); }
fallo() { printf '\033[31m❌ FALLO\033[0m : %s\n' "$1"; FALLOS=$((FALLOS+1)); }

# casa <descripcion> <fichero> <patron>
casa() {
	if grep -qF -- "$3" "$2"; then ok "$1"; else
		fallo "$1"
		printf '            esperaba encontrar: %s\n' "$3"
	fi
}

# aborta <descripcion> <patron esperado en el error> -- <comando...>
aborta() {
	local desc="$1" patron="$2"; shift 3
	local salida rc
	salida="$("$@" 2>&1)"; rc=$?
	if [ "$rc" -eq 0 ]; then
		fallo "$desc (devolvio 0; tenia que abortar)"
	elif ! printf '%s' "$salida" | grep -qi -- "$patron"; then
		fallo "$desc (aborto, pero sin explicar '$patron')"
		printf '            dijo: %s\n' "$(printf '%s' "$salida" | head -2)"
	else
		ok "$desc"
	fi
}

echo "### Conversion a YAML de gcosts"

$INV convertir --csv "$AQUI/t/inventario.csv" --dir "$TMP/caso" \
	--region us-central1 --proyecto prueba >/dev/null 2>&1
Y="$TMP/caso/01-inventario.yml"

if [ ! -f "$Y" ]; then
	fallo "no se genero el YAML; el resto de la conversion no se puede comprobar"
else
	casa "region por defecto en cabecera"          "$Y" "region: us-central1"
	casa "proyecto en cabecera"                    "$Y" "project: prueba"
	casa "vm sin licencia NO lleva campo os"       "$Y" "name: vm-libre"
	casa "vm con rhel si lo lleva"                 "$Y" "os: rhel"
	casa "compromiso -> commitment"                "$Y" "commitment: 3"
	casa "disco con padre cuelga de su vm"         "$Y" "name: vm-libre-boot"
	casa "disco suelto va en su propia seccion"    "$Y" "name: suelto"
	casa "bucket con su clase"                     "$Y" "class: standard"
	if grep -q "os: $" "$Y" || grep -q "os: free" "$Y"; then
		fallo "se colo un 'os' vacio o inventado en el YAML"
	else
		ok "ningun 'os' vacio ni inventado"
	fi
fi

echo
echo "### Importes"

$INV calcular --csv "$AQUI/t/inventario.csv" --region us-central1 \
	--tarifas "$AQUI/t/tarifas.yml" --costes-gcosts "$AQUI/t/costs-gcosts.csv" \
	--salida "$TMP/costes.csv" >"$TMP/salida.txt" 2>&1
C="$TMP/costes.csv"

if [ ! -f "$C" ]; then
	fallo "no se genero el CSV de costes"
	cat "$TMP/salida.txt"
else
	casa "stock mensual:  10000 GiB x 0,023        = 230,00"  "$C" ",230.0"
	casa "consumo:            4 TiB x 6,25         = 25,00"   "$C" ",25.0"
	casa "hora-permanente:    2 nodos x 0,65 x 730 = 949,00"  "$C" ",949.0"
	casa "factor 730 aplicado solo donde toca"     "$C" ",730,949.0"
	casa "coste de gcosts arrastrado tal cual"     "$C" ",226.92,"
	casa "el motor queda marcado en cada linea"    "$C" "gcosts,"
	casa "el SKU queda registrado"                 "$C" "TEST-0003"
	casa "total de las tarifas"        "$TMP/salida.txt" "1,204.00"
	casa "total general"               "$TMP/salida.txt" "1,440.92"
	casa "aviso de precios de lista"   "$TMP/salida.txt" "Precios de lista"
fi

echo
echo "### Guardarrailes (tienen que abortar)"

printf 'tipo,nombre,region,spec,cantidad\nvm,x,us-central1,n2-standard-8,\n' >"$TMP/so.csv"
printf 'tipo,nombre,region,spec,cantidad,so\nvm,x,us-central1,n2-standard-8,,ubuntu-pro\n' >"$TMP/so2.csv"
aborta "so que gcosts no cobra" "no lo reconoce" -- \
	$INV convertir --csv "$TMP/so2.csv" --dir "$TMP/o1" --region us-central1

printf 'tipo,nombre,region,cantidad\ninventado,x,us-central1,1\n' >"$TMP/tipo.csv"
aborta "tipo de recurso desconocido" "desconocido" -- \
	$INV convertir --csv "$TMP/tipo.csv" --dir "$TMP/o2" --region us-central1

printf 'tipo,nombre,region,spec,cantidad,padre\ndisco,d,us-central1,ssd,10,no-existe\n' >"$TMP/huerfano.csv"
aborta "disco colgado de una vm que no existe" "no esta en el inventario" -- \
	$INV convertir --csv "$TMP/huerfano.csv" --dir "$TMP/o3" --region us-central1

printf 'tipo,nombre,region,spec,cantidad\nbucket,b,us-central1,standard,mucho\n' >"$TMP/nan.csv"
aborta "cantidad que no es un numero" "no es un numero" -- \
	$INV convertir --csv "$TMP/nan.csv" --dir "$TMP/o4" --region us-central1

printf 'tipo,nombre,coste\nbucket,b,3\n' >"$TMP/col.csv"
aborta "columna que no existe en el esquema" "no reconocidas" -- \
	$INV convertir --csv "$TMP/col.csv" --dir "$TMP/o5" --region us-central1

sed 's/periodo: hora-permanente/periodo: hora/' "$AQUI/t/tarifas.yml" >"$TMP/viejas.yml"
aborta "tarifas.yml con un periodo de otra version" "no conoce" -- \
	$INV calcular --csv "$AQUI/t/inventario.csv" --region us-central1 \
		--tarifas "$TMP/viejas.yml" --costes-gcosts "$AQUI/t/costs-gcosts.csv" \
		--salida "$TMP/x.csv"

printf 'a,b,c\n1,2,3\n' >"$TMP/malo.csv"
aborta "costs.csv que no es de gcosts" "no parece un costs.csv" -- \
	$INV calcular --csv "$AQUI/t/inventario.csv" --region us-central1 \
		--tarifas "$AQUI/t/tarifas.yml" --costes-gcosts "$TMP/malo.csv" \
		--salida "$TMP/x.csv"

# La region de la fila manda sobre --region. Para probar que falta la tarifa hay
# que dejar la columna region vacia, si no se resolveria por us-central1.
printf 'tipo,nombre,region,cantidad\nbigquery-consulta,q,,4\n' >"$TMP/sinregion.csv"
aborta "region sin tarifas cargadas" "no hay tarifa" -- \
	$INV calcular --csv "$TMP/sinregion.csv" --region europe-west1 \
		--tarifas "$AQUI/t/tarifas.yml" --salida "$TMP/x.csv"

printf 'tipo,nombre,region,cantidad\nbigquery-consulta,q,,4\n' >"$TMP/regionfila.csv"
if $INV calcular --csv "$TMP/regionfila.csv" --region us-central1 \
		--tarifas "$AQUI/t/tarifas.yml" --salida "$TMP/y.csv" >/dev/null 2>&1; then
	ok "la region vacia cae en la --region por defecto"
else
	fallo "la region vacia deberia caer en la --region por defecto"
fi

aborta "inventario con recursos de gcosts pero sin su costs.csv" "no se indico" -- \
	$INV calcular --csv "$AQUI/t/inventario.csv" --region us-central1 \
		--tarifas "$AQUI/t/tarifas.yml" --salida "$TMP/x.csv"

echo
echo "### Perfiles: inventario de cliente -> esquema canonico"

$INV normalizar --csv "$AQUI/t/cliente.csv" --perfil "$AQUI/t/perfil.yml" \
	--region us-central1 --salida "$TMP/norm.csv" >"$TMP/norm.txt" 2>&1
N="$TMP/norm.csv"

if [ ! -f "$N" ]; then
	fallo "la normalizacion no produjo CSV"
	cat "$TMP/norm.txt"
else
	casa "una fila de cliente da vm y disco"      "$N" "APP-01-disco"
	if grep -q "APP-03" "$N"; then
		fallo "filtro: APP-03 esta apagada y no debia aparecer"
	else
		ok "filtro: la VM apagada no aparece"
	fi
	casa "MiB -> GiB: 512000 MiB = 500 GiB"       "$N" ",500.0,"
	casa "sizing 8 vCPU / 32 GiB -> el mas barato" "$N" "e2-standard-8"
	casa "sizing 2 vCPU / 8 GiB"                   "$N" "e2-standard-2"
	casa "mapa de SO: Red Hat -> rhel"             "$N" ",rhel,"
	if grep -q "APP-02,us-central1,e2-standard-2,,,," "$N"; then
		ok "cajon de sastre: Ubuntu queda sin licencia"
	else
		fallo "cajon de sastre: Ubuntu debia quedar sin licencia"
	fi
	if grep -q "APP-02-disco" "$N"; then
		fallo "omitir_si_vacio: APP-02 no tiene disco y no debia generar fila"
	else
		ok "omitir_si_vacio: sin disco, no hay fila de disco"
	fi
fi

printf 'A;B\nx;1\n' >"$TMP/mini.csv"
cat >"$TMP/malcol.yml" <<'YML2'
csv: {delimitador: ';'}
salidas:
  - tipo: {constante: vm}
    nombre: {desde: 'NoExiste'}
YML2
aborta "perfil que apunta a una columna inexistente" "no tiene la columna" -- \
	$INV normalizar --csv "$TMP/mini.csv" --perfil "$TMP/malcol.yml" --salida "$TMP/z.csv"

cat >"$TMP/malmapa.yml" <<'YML3'
csv: {delimitador: ';'}
salidas:
  - tipo: {constante: vm}
    nombre: {desde: 'A'}
    so: {desde: 'A', mapa: {'*zzz*': rhel}}
YML3
aborta "valor que no casa con ningun patron del mapa" "no casa con ningun patron" -- \
	$INV normalizar --csv "$TMP/mini.csv" --perfil "$TMP/malmapa.yml" --salida "$TMP/z.csv"

cat >"$TMP/malcampo.yml" <<'YML4'
csv: {delimitador: ';'}
salidas:
  - tipo: {constante: vm}
    nombre: {desde: 'A'}
    inventado: {constante: x}
YML4
aborta "perfil con un campo que no es canonico" "no es un campo" -- \
	$INV normalizar --csv "$TMP/mini.csv" --perfil "$TMP/malcampo.yml" --salida "$TMP/z.csv"

cat >"$TMP/gpu.yml" <<'YML5'
csv: {delimitador: ';'}
salidas:
  - tipo: {constante: vm}
    nombre: {desde: 'A'}
    spec: {sizing: {vcpu: 'B', ram_gb: 'B', familia: 'noexiste'}}
YML5
aborta "familia inexistente en el sizing" "no hay tipo de maquina" -- \
	$INV normalizar --csv "$TMP/mini.csv" --perfil "$TMP/gpu.yml" --salida "$TMP/z.csv"

echo
printf '🧪 PRUEBAS : %s\n' "$((OK+FALLOS))"
if [ "$FALLOS" -eq 0 ]; then
	printf '\033[32m✅ DONE  : All successful\033[0m\n'
	exit 0
fi
printf '\033[31m❌ FALLOS : %s\033[0m\n' "$FALLOS"
exit 1
