# Plan: `gcosts` en Cloud Run Job, con precios regenerados y validados en tu fork

## Contexto

Se necesita ejecutar `gcosts` en GCP, lanzado por el equipo técnico desde CLI/CI, con la garantía
de que los precios vienen de la fuente oficial de Google y están verificados.

Medición que fija el diseño (comparé el `pricing.yml` del 13/08 con el del 20/08 de upstream):

| Clave | Líneas cambiadas en 7 días |
|---|---|
| `month_spot` / `hour_spot` | ~55.000 |
| `month` / `hour` (on-demand) | 78 |
| `month_1y` / `month_3y` (CUD) | 83 |

Sobre 2.010 precios mensuales on-demand (todos los tipos × 4 regiones): **0 cambios**. Los precios
de lista on-demand y CUD son casi inmóviles; lo volátil es Spot — que además **se excluye de las
propuestas** por decisión tomada, al no ser un precio comprometible.

Conclusión: no hace falta consultar en vivo (imposible sin reejecutar el pipeline entero, porque
la Billing API devuelve SKUs sueltos y el precio por tipo de máquina es una derivación de este
proyecto). Basta con **regenerar en tu fork con tu propia API key, pasando los guardarraíles que
ya existen**, y hornear el resultado validado en la imagen.

## Arquitectura

```
 Fork BetinMcFly (GitHub Actions, cron)        GCP claude-projects-496723
 ┌────────────────────────────────────┐
 │ build-pricing.yml                  │
 │  ├ skus.sh ──API key──▶ Cloud Billing Catalog API   (fuente oficial)
 │  ├ pricing.pl → pricing.yml        │
 │  ├ t/test.sh    (543 asertos)  ◀── guardarraíl
 │  ├ t/diffcheck.sh              ◀── guardarraíl
 │  └ commit + push a master          │
 └───────────────┬────────────────────┘
                 │ push
                 ▼
        gcloud builds submit ──▶ Artifact Registry ──▶ Cloud Run Job
        (hornea pricing.yml)                              │ Cloud Storage FUSE
                                                    gs://…/casos/<cliente>/
                                                      *.yml → costs.csv
```

La imagen solo se construye **después** de que los 543 asertos y el diffcheck pasen. Si una
regeneración sale mal, no llega a producción.

## Paso 0 — Dejar el plan en el repo para revisión (antes de nada)

Primera acción, y **puramente local**: escribir este plan en `deploy/PLAN.md` y añadir en
`CLAUDE.md` una línea que lo referencie. Nada se empuja a GitHub ni se crea en GCP hasta que lo
revises y des el visto bueno explícito a los pasos con efecto externo.

## Paso 1 — Conectar el clon al fork

```bash
git remote set-url origin git@github.com:BetinMcFly/google-cloud-pricing-cost-calculator.git
git remote add upstream https://github.com/Cyclenerd/google-cloud-pricing-cost-calculator.git
git pull upstream master          # trae el pricing.yml del 20/08
git checkout -b deploy/cloud-run
```

`master` queda como espejo de upstream **y** como rama donde el workflow commitea los precios
regenerados. Nuestro trabajo de despliegue vive en `deploy/cloud-run` hasta fusionarlo.

## Paso 2 — Credencial de Billing en GCP

```bash
gcloud services enable cloudbilling.googleapis.com
```
Crear una API key **restringida exclusivamente a la Cloud Billing API** (sin otras APIs, sin
restricción de IP porque la usa GitHub Actions). Las consultas al catálogo no se facturan.

## Paso 3 — Preparar el fork en GitHub

- **Habilitar Actions** (GitHub las desactiva por defecto en forks) y permitir que el workflow
  escriba en el repo (necesita hacer `git push` del `pricing.yml`).
- Guardar la API key como secret `API_KEY` — es el nombre que `build-pricing.yml` ya espera.
- **Desactivar los workflows que no aplican al fork**, o llenarán el repo de fallos:
  - `check-changes.yml`: autentica contra el *proyecto de upstream*
    (`projects/586925744942/...`, SA `gh-cost-calculator@billing-api-340811`). En tu fork falla
    siempre. Además abre issues automáticamente.
  - `release.yml` / `pre-release.yml`: publican releases de binarios, que no necesitas.
- Ajustar el cron de `build-pricing.yml` si quieres más frecuencia que el jueves. Dado que
  on-demand y CUD apenas se mueven, **semanal es suficiente**; lo que aporta valor es poder
  dispararlo a mano (`workflow_dispatch`, ya soportado) antes de una propuesta importante.

**Nada del pipeline de precios se modifica.** Solo configuración del repo.

## Paso 4 — Contenedor (ficheros nuevos)

| Fichero | Contenido |
|---|---|
| `Dockerfile` | Multi-stage: `golang:1.24` compila con `CGO_ENABLED=0`; runtime `gcr.io/distroless/static-debian12:nonroot` con solo el binario y `pricing.yml`. Sin Perl/SQLite/gcloud. `ENTRYPOINT ["/gcosts"]`, sin `CMD`. ≈17 MB |
| `.dockerignore` | Deja fuera de la imagen todo lo que no sea `gcosts/` y `pricing.yml`, **`t/` incluido**: la prueba de aceptación no corre dentro del contenedor, sino que `deploy/cloudbuild.yaml` le monta `/workspace/t` del checkout de Cloud Build |
| `deploy/cloudbuild.yaml` | Build + push a Artifact Registry (`.yaml`; con `.yml` lo descartaría el `.gitignore`) |
| `deploy/README.md` | Runbook: build, deploy, ejecución, y cómo forzar una regeneración de precios |
| `deploy/PLAN.md` | Este plan (creado en el Paso 0) |
| `CLAUDE.md` | Ya creado; se le añade el puntero a `deploy/PLAN.md` |

## Paso 5 — Infraestructura y despliegue

```bash
gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com
gcloud artifacts repositories create gcosts --repository-format=docker --location=us-central1
gcloud storage buckets create gs://gcosts-casos-<sufijo> --location=us-central1
```

- **Build manual con `gcloud builds submit`** (ver `deploy/README.md` §1). Se descartó el trigger
  automático de Cloud Build: conectarlo al repo exige autorizar su app de GitHub por OAuth desde el
  navegador, y como la regeneración es semanal, no compensa esa integración extra. Ventaja añadida:
  ninguna credencial cruzada entre GitHub y GCP.
- El pipeline de build (`deploy/cloudbuild.yaml`) **valida antes de publicar**: construye la imagen,
  calcula los 57 fixtures de `t/` con ella y ejecuta `t/test.sh`. Si los ~543 asertos no pasan, la
  imagen no se sube.
- Imagen etiquetada con la fecha de `about.generated` del YAML horneado, además de la versión.
- **Cloud Run Job** `gcosts`: 512 MiB, 1 vCPU, timeout 5 min, `--max-retries=1`, service account
  propia con `roles/storage.objectUser` **solo** sobre el bucket. Ningún permiso de Billing: el job
  no consulta precios. Volumen GCS montado en `/mnt/casos`.

Ejecución:
```bash
gcloud run jobs execute gcosts --region=us-central1 \
  --args=calc,--dir=/mnt/casos/cliente-x,--pricing=/pricing.yml,--csv=/mnt/casos/cliente-x/costs.csv
```

⚠️ El directorio montado debe contener **solo** ficheros de uso: `calc` parsea todos los `*.yml`
del directorio y falla si encuentra la lista de precios. Por eso `pricing.yml` vive en `/`.

## Paso 6 — Convenciones de uso

- **Sin Spot en propuestas**: no usar `spot: true` en los YAML de cliente. El runbook incluye un
  `grep -r 'spot:' <dir>` como comprobación previa a ejecutar el job.
- **Aviso de frescura** (elegiste avisar, no bloquear): el runbook ejecuta `gcosts about` antes del
  cálculo para dejar en el log la fecha del `pricing.yml` usado. Si más adelante quieres que sea
  automático, hay que añadir la comprobación en Go — hoy `about` solo imprime `About.Generated` y
  el campo `timestamp` se deserializa pero no se usa.
- **Nunca `--download`**: `ensurePricingFile()` (`gcosts/cmd/root.go`) **falla abierto** — si la
  descarga falla, avisa y sigue con el fichero por defecto, así que un cálculo podría continuar con
  precios equivocados. Además cachea en `os.TempDir()`, que en Cloud Run es tmpfs en memoria.
- Los casos de cliente viven en GCS, no en git: `.gitignore` descarta `**.yml` y `**.csv`
  globalmente y `usage/customers/**` explícitamente.

## Verificación

Sin Go, Docker ni make en esta máquina — el build ocurre en Cloud Build y la validación usa `bash`,
que sí está disponible.

1. ✅ **El pipeline de precios sigue sano en tu fork** — lanzado a mano el 22/08/2026: los 543
   asertos y el diffcheck pasaron, y commiteó `Pricing updated` (`f510ec1`) en `master`.
2. ✅ **La contenerización no altera ningún número** — prueba de aceptación principal, superada
   con el CSV que produjo el propio job: `🧪 TESTS : 543` / `✅ DONE : All successful`.
   ```bash
   gcloud storage cp t/*.yml gs://gcosts-casos-<sufijo>/t/
   gcloud run jobs execute gcosts --region=us-central1 \
     --args=calc,--dir=/mnt/casos/t,--pricing=/pricing.yml,--csv=/mnt/casos/t/costs.csv
   gcloud storage cp gs://gcosts-casos-<sufijo>/t/costs.csv t/costs.csv
   cd t && bash test.sh     # debe dar ✅ DONE : All successful
   ```
3. ⛔ **La memoria real no es medible con este job, y los 512 MiB se quedan como están.**

   Se consultó Cloud Monitoring para la ejecución del 22/08/2026 (22:35:22 → 22:37:55, 2m33s):

   | Métrica | Muestras |
   |---|---|
   | `run.googleapis.com/job/completed_task_attempt_count` | 1 — la ejecución sí quedó registrada |
   | `run.googleapis.com/container/memory/utilizations` | **0** |
   | `run.googleapis.com/container/cpu/utilizations` | **0** |

   Las distribuciones de utilización se muestrean periódicamente y el job termina antes de que se
   tome ninguna muestra. No es un fallo de configuración ni algo que arregle repetir la ejecución:
   mientras el cálculo dure un par de minutos, esas series seguirán vacías.

   **Decisión: no perseguir el número.** Bajar el límite a 256 MiB ahorraría una fracción de
   céntimo por ejecución en un job que se lanza puntualmente, a cambio de arriesgar un OOM en
   mitad de una propuesta. Los 512 MiB siguen siendo una estimación sobre 4,1 MB de YAML, y se
   asumen como tal.

   Si algún día el job pasa a ejecutarse con frecuencia y el límite empieza a importar, la única
   vía practicable es empírica: bajar `--memory` a propósito hasta que la ejecución falle, y subir
   un escalón desde ahí.

## Lo que esto no resuelve (y conviene decir en las propuestas)

- Son **precios de lista públicos**. Acuerdos empresariales y descuentos negociados no están en la
  Billing API; para eso existe el campo `discount` en los YAML de uso.
- El repo documenta desviaciones conocidas: `t/README.md` registra que las M2 grandes calculan por
  encima de la calculadora oficial, y `t/test.sh` anota que en `africa-south1` el precio de lista
  de `n2-standard-8` no cuadra con los SKUs verificados.
- El free tier se ignora deliberadamente (documentado en el README de upstream).

## Acciones con efecto externo (requieren tu visto bueno)

- `git push` a tu fork; cambios de configuración en el repo de GitHub (secret, Actions).
- Crear en `claude-projects-496723`: API key de Billing, Artifact Registry, bucket, service
  account y Cloud Run Job. Coste pequeño pero real (almacenamiento de imagen,
  minutos de build, ejecuciones por segundo); las consultas al catálogo de Billing son gratuitas.
