#!/usr/bin/env bash
#
# De un CSV de inventario a un CSV de costes, en un solo comando.
#
#   bash calcular.sh <inventario.csv> <nombre-del-caso> [region]
#
# Hace el viaje entero: convierte, sube el YAML al bucket, ejecuta el Cloud Run
# Job, se trae el costs.csv y lo une con los servicios que gcosts no precia.
#
# Requiere gcloud autenticado. La region por defecto es us-central1.

set -euo pipefail

CSV="${1:-}"
CASO="${2:-}"
REGION="${3:-us-central1}"
PERFIL="${4:-}"

if [ -z "$CSV" ] || [ -z "$CASO" ]; then
	cat >&2 <<'USO'
uso: bash calcular.sh <csv> <nombre-del-caso> [region] [perfil.yml]

  Sin perfil, el CSV tiene que venir ya en el esquema canonico
  (tipo,nombre,region,spec,cantidad,...). Con perfil, se acepta el CSV tal
  como lo entrego el cliente y se traduce antes de calcular.

  Para escribir el perfil de un cliente nuevo:
    python3 inventario.py inspeccionar --csv suyo.csv --borrador perfiles/x.yml
USO
	exit 1
fi
[ -f "$CSV" ] || { echo "no existe $CSV" >&2; exit 1; }

AQUI="$(cd "$(dirname "$0")" && pwd)"
PROYECTO="claude-projects-496723"
BUCKET="gcosts-casos-${PROYECTO}"
JOB="gcosts"
DESTINO="gs://${BUCKET}/casos/${CASO}"
TRABAJO="$(mktemp -d)"
trap 'rm -rf "$TRABAJO"' EXIT

if [ -n "$PERFIL" ]; then
	[ -f "$PERFIL" ] || { echo "no existe el perfil $PERFIL" >&2; exit 1; }
	echo "▶ 0/5  Traduciendo el inventario del cliente con $PERFIL"
	CANONICO="$TRABAJO/canonico.csv"
	python3 "$AQUI/inventario.py" normalizar \
		--csv "$CSV" --perfil "$PERFIL" --region "$REGION" --salida "$CANONICO"
	ORIGINAL="$CSV"
	CSV="$CANONICO"
	echo
fi

echo "▶ 1/5  Convirtiendo el inventario"
python3 "$AQUI/inventario.py" convertir \
	--csv "$CSV" --dir "$TRABAJO/caso" --region "$REGION" --proyecto "$CASO"

# Puede no haber nada para gcosts: un inventario solo de BigQuery es legitimo.
if [ -f "$TRABAJO/caso/01-inventario.yml" ]; then
	echo
	echo "▶ 2/5  Subiendo a ${DESTINO}"
	# El directorio del job debe contener SOLO ficheros de uso: 'calc' parsea
	# todos los *.yml que encuentra y aborta si tropieza con otra cosa.
	gcloud storage rm "${DESTINO}/**" --quiet 2>/dev/null || true
	gcloud storage cp "$TRABAJO/caso/01-inventario.yml" "${DESTINO}/" --quiet

	echo
	echo "▶ 3/5  Ejecutando el Cloud Run Job (tarda un par de minutos)"
	gcloud run jobs execute "$JOB" --region=us-central1 --wait --quiet \
		--args=calc,--dir=/mnt/casos/casos/"${CASO}",--pricing=/pricing.yml,--csv=/mnt/casos/casos/"${CASO}"/costs.csv

	echo
	echo "▶ 4/5  Recogiendo el resultado"
	gcloud storage cp "${DESTINO}/costs.csv" "$TRABAJO/costs-gcosts.csv" --quiet
	GCOSTS=(--costes-gcosts "$TRABAJO/costs-gcosts.csv")
else
	echo "   (nada que calcular con gcosts en este inventario)"
	GCOSTS=()
fi

echo
echo "▶ 5/5  Uniendo ambos motores"
SALIDA="${ORIGINAL:-$CSV}"
SALIDA="${SALIDA%.csv}-costes.csv"
python3 "$AQUI/inventario.py" calcular \
	--csv "$CSV" --region "$REGION" \
	--tarifas "$AQUI/tarifas.yml" \
	"${GCOSTS[@]}" --salida "$SALIDA"

echo
echo "Resultado en: $SALIDA"
