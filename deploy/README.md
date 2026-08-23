# Runbook: gcosts en Cloud Run Job

Operativa del dia a dia. Para la configuracion inicial (una sola vez) ver
[`SETUP.md`](SETUP.md). Para el porque de cada decision, [`PLAN.md`](PLAN.md).

Proyecto: `claude-projects-496723` · Region: `us-central1`

---

## 1. Construir y publicar la imagen

```bash
# El tag debe ser la fecha de generacion del pricing.yml que se hornea.
# Consultarla primero:
grep -m1 '^  generated:' pricing.yml      # -> generated: Thu Aug 20 04:22:20 2026

gcloud builds submit --config deploy/cloudbuild.yaml --substitutions=_TAG=2026-08-20
```

El build hace tres cosas, en este orden:

1. Compila `gcosts` y monta la imagen con `pricing.yml` dentro.
2. Calcula los 57 ficheros de `t/` con esa imagen.
3. Ejecuta `t/test.sh` sobre el CSV resultante.

**Si el paso 3 falla, la imagen no se publica.** Es la garantia de que contenerizar no altero
ningun precio: los ~543 asertos estan contrastados contra la calculadora oficial de Google.

---

## 2. Desplegar el job (solo la primera vez, o al cambiar la config)

> **Anclar al digest, no al tag.** Un tag puede reescribirse por un build posterior y cambiar en
> silencio los precios de un job en produccion. El digest es inmutable:
> `gcloud artifacts docker images describe IMAGEN:TAG --format='value(image_summary.digest)'`

```bash
gcloud run jobs deploy gcosts \
  --image=us-central1-docker.pkg.dev/claude-projects-496723/gcosts/gcosts@sha256:DIGEST \
  --region=us-central1 \
  --memory=512Mi --cpu=1 --task-timeout=5m --max-retries=1 \
  --service-account=gcosts-job@claude-projects-496723.iam.gserviceaccount.com \
  --add-volume=name=casos,type=cloud-storage,bucket=gcosts-casos-claude-projects-496723 \
  --add-volume-mount=volume=casos,mount-path=/mnt/casos
```

> **Los 512 MiB son una estimacion, no una medicion.** El job termina en ~2m30s, antes de que
> Cloud Run tome ninguna muestra de `container/memory/utilizations`, asi que esa serie sale vacia
> y el consumo real no es observable. Se deja holgado a proposito: ver el punto 3 de
> *Verificacion* en `deploy/PLAN.md`.

Para actualizar solo la imagen mas adelante:

```bash
gcloud run jobs update gcosts --region=us-central1 \
  --image=us-central1-docker.pkg.dev/claude-projects-496723/gcosts/gcosts@sha256:NUEVO_DIGEST
```

---

## 3. Calcular un caso

### Preparar los ficheros

```bash
gcloud storage cp mi-caso/*.yml gs://gcosts-casos-claude-projects-496723/casos/cliente-x/
```

> **El directorio debe contener SOLO ficheros de uso.** `calc` parsea todos los `*.yml` que
> encuentra; si `pricing.yml` acaba ahi, el proceso falla al intentar interpretarlo como fichero
> de uso. Por eso los precios viven en `/pricing.yml`, dentro de la imagen.

### Comprobaciones previas

```bash
# 1. Sin precios Spot: no son comprometibles en una propuesta
grep -r 'spot:' mi-caso/ && echo "!! QUITAR SPOT ANTES DE SEGUIR"

# 2. Dejar constancia de que precios se van a usar
gcloud run jobs execute gcosts --region=us-central1 --args=about --wait
```

### Ejecutar

```bash
gcloud run jobs execute gcosts --region=us-central1 --wait \
  --args=calc,--dir=/mnt/casos/casos/cliente-x,--pricing=/pricing.yml,--csv=/mnt/casos/casos/cliente-x/costs.csv

gcloud storage cp gs://gcosts-casos-claude-projects-496723/casos/cliente-x/costs.csv .
```

**Salida distinta de cero significa que no hay CSV.** `ExportCsv` corre al final del proceso, asi
que si falta un precio el job aborta (`os.Exit(1)`) sin escribir nada. No hay resultado parcial
que rescatar: hay que mirar el log, corregir y repetir.

---

## 4. Actualizar los precios

Los precios se regeneran en el fork, no aqui. Ver [`SETUP.md`](SETUP.md) parte F.

```bash
# Forzar una regeneracion (antes de una propuesta importante)
gh workflow run "Build" --repo BetinMcFly/google-cloud-pricing-cost-calculator

# Cuando termine y haya commiteado, traerla y reconstruir la imagen
git checkout master && git pull origin master
grep -m1 '^  generated:' pricing.yml     # anota la fecha para el tag
gcloud builds submit --config deploy/cloudbuild.yaml --substitutions=_TAG=NUEVA_FECHA
gcloud run jobs update gcosts --region=us-central1 --image=...:NUEVA_FECHA
```

De forma automatica, el workflow corre los jueves a las 03:45 UTC.

---

## 5. Problemas conocidos

### `permission denied` al escribir el CSV en el volumen

La imagen corre como usuario `nonroot` (uid 65532) y el montaje de GCS puede pertenecer a otro
uid. Solucion: fijar el uid en las opciones de montaje al desplegar.

```bash
gcloud run jobs update gcosts --region=us-central1 \
  --add-volume=name=casos,type=cloud-storage,bucket=gcosts-casos-claude-projects-496723,mount-options="uid=65532,gid=65532"
```

### `Google Cloud region '...' not found!`

La region del YAML de uso no existe en `pricing.yml`. Listar las validas:

```bash
gcloud run jobs execute gcosts --region=us-central1 --args=region --wait
```

### El calculo usa precios viejos

`gcosts about` imprime la fecha de generacion del `pricing.yml` de la imagen. Si no coincide con
lo esperado, la imagen desplegada es antigua: reconstruir (seccion 4).

**Nunca usar `--download`.** `ensurePricingFile()` falla abierto: si la descarga no funciona,
avisa y sigue con el fichero por defecto, de modo que el calculo continuaria con precios
equivocados en vez de abortar. Ademas cachea en `os.TempDir()`, que en Cloud Run es tmpfs en
memoria y se pierde en cada arranque en frio.

---

## 6. Lo que estos numeros NO son

- **Precios de lista publicos.** Acuerdos empresariales y descuentos negociados no estan en la
  Billing API. Para eso existe el campo `discount` en los YAML de uso.
- El repo documenta desviaciones conocidas: `t/README.md` registra que las M2 grandes calculan por
  encima de la calculadora oficial, y `t/test.sh` anota que en `africa-south1` el precio de lista
  de `n2-standard-8` no cuadra con los SKUs verificados.
- El free tier se ignora deliberadamente.

---

## 7. Estructura del bucket y respaldos

`gs://gcosts-casos-claude-projects-496723/` tiene dos prefijos con proposito distinto:

| Prefijo | Contenido |
|---|---|
| `casos/<cliente>/` | Ficheros de uso `*.yml` y su `costs.csv`. Es lo que se le pasa al job en `--dir` |
| `respaldos/` | Segunda copia del arbol de trabajo de `propuestas-gcp`. **Nunca apuntar `--dir` aqui** |

El job solo lee el directorio que se le indica en `--dir`, asi que `respaldos/` no interfiere con
ningun calculo. La regla de "solo ficheros de uso en el directorio" (seccion 3) sigue aplicando
dentro de `casos/`.

### Restaurar `propuestas-gcp` en una maquina nueva

La copia canonica ya no es el bucket: los patrones, plantillas, rate card, referencia y las
propuestas de cliente viven en el repo privado `BetinMcFly/propuestas-gcp`. Restaurar es clonarlo:

```bash
git clone git@github.com:BetinMcFly/propuestas-gcp.git ~/propuestas-gcp/data
```

El prefijo `respaldos/propuestas-gcp/` del bucket queda como **segunda copia del arbol de
trabajo**, para el caso de perder el acceso a GitHub y para conservar lo que aun no se ha
commiteado. Se sincroniza **excluyendo `.git/`**: un `.git` copiado a medias es peor que ninguno,
porque aparenta ser un repo valido sin serlo. Si se restaura desde el bucket, se obtiene el arbol
de ficheros sin historia; para tener historia, clonar.

```bash
# Respaldar (VM -> bucket)
gcloud storage rsync -r -x '.*\.git/.*' \
  ~/propuestas-gcp gs://gcosts-casos-claude-projects-496723/respaldos/propuestas-gcp

# Recuperar (bucket -> VM), mismo comando con origen y destino invertidos
gcloud storage rsync -r -x '.*\.git/.*' \
  gs://gcosts-casos-claude-projects-496723/respaldos/propuestas-gcp ~/propuestas-gcp
```

Verificar con `--dry-run`: si no lista ninguna copia pendiente, ambos lados son identicos.

> El `-x` de `gcloud storage rsync` es un regex **anclado al principio** de la ruta, no una
> busqueda parcial. Por eso `.*\.git/.*` y no `\.git/`, que no excluye nada y deja el respaldo
> lleno de objetos sueltos de git.
