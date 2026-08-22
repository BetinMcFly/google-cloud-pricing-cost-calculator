# Configuración inicial (una sola vez)

Guía paso a paso para dejar operativo el pipeline de precios en el fork
`BetinMcFly/google-cloud-pricing-cost-calculator` y su proyecto de GCP
`claude-projects-496723`.

Se hace **una sola vez**. Después, la regeneración de precios es automática (cron semanal) o
manual con un comando.

> Casi todo se puede hacer por línea de comandos. Se indica también la ruta equivalente en la
> consola web por si prefieres verlo, o para comprobar el resultado.

**Orden importante:** la Parte B debe ir antes que la D y la E, porque hasta que no se habilitan
las Actions del fork no existe ningún workflow sobre el que actuar.

---

## Parte A — Crear la API key de Cloud Billing (en GCP)

### Qué es y por qué

El pipeline (`build/skus.sh`) consulta el **Cloud Billing Catalog API**, que es el catálogo
**público** de precios de lista de Google. No expone datos de tu facturación ni de tus proyectos:
solo la lista de SKUs y sus precios. Por eso basta una API key simple, sin cuenta de servicio.

Aun así la restringimos a esa única API: si la clave se filtrase, no serviría para nada más.

### Comandos

```bash
# 1. Confirmar el proyecto activo
gcloud config get-value project        # debe decir: claude-projects-496723

# 2. Habilitar las dos APIs necesarias
#    cloudbilling = el catálogo de precios | apikeys = poder crear claves por CLI
gcloud services enable cloudbilling.googleapis.com apikeys.googleapis.com

# 3. Crear la clave, restringida a la Billing API
gcloud services api-keys create \
  --display-name="gcosts-billing-catalog" \
  --api-target=service=cloudbilling.googleapis.com

# 4. Listar las claves y quedarte con el UID de la recién creada
gcloud services api-keys list --format="table(uid, displayName)"

# 5. Obtener el valor de la clave (esto es el secreto)
gcloud services api-keys get-key-string <UID>
```

El paso 5 imprime algo como `AIzaSy...`. **Ese valor es el secreto**; guárdalo para la Parte D.

### Equivalente en la consola web

APIs y servicios → Credenciales → Crear credenciales → Clave de API → Restringir clave →
Restricciones de API → seleccionar *Cloud Billing API*.

### Verificar que funciona

```bash
curl -s "https://cloudbilling.googleapis.com/v1/services?key=TU_CLAVE" | head -c 300
```

Debe devolver JSON con una lista de servicios. Si devuelve un error `403`, la clave no tiene bien
puesta la restricción o la API no está habilitada.

### Seguridad

- **No la commitees nunca.** El `.gitignore` del repo ya descarta `**.conf` (que es donde
  `skus.sh` la buscaría en local, en un fichero `build/skus.conf`).
- Vive solo en dos sitios: tu gestor de contraseñas y el secret de GitHub.
- Las consultas al catálogo **no se facturan**.

---

## Parte B — Habilitar las Actions del fork (único paso que exige el navegador)

### Por qué es manual

Cuando forkeas un repositorio, GitHub **no activa** los workflows heredados: un fork podría
contener workflows escritos por otra persona, y ejecutarlos automáticamente sería un riesgo. Por
eso exige una confirmación humana explícita, y **no hay API ni comando de `gh` para saltársela**.

Estado actual comprobado: `0` workflows registrados en el fork.

### Pasos

1. Abre: <https://github.com/BetinMcFly/google-cloud-pricing-cost-calculator/actions>
2. Verás un aviso amarillo: *"Workflows aren't being run on this forked repository"*.
3. Pulsa el botón verde **"I understand my workflows, go ahead and enable them"**.

### Verificar

```bash
gh api repos/BetinMcFly/google-cloud-pricing-cost-calculator/actions/workflows \
  --jq '.workflows[] | "\(.state)\t\(.name)"'
```

> Usa `gh api`, **no** `gh workflow list`: este último solo muestra los workflows *activos* y
> oculta los desactivados, que es justo lo que necesitas ver aquí.

Antes devolvía `0`. Ahora aparecen los cinco, pero **no todos activos**:

```
disabled_fork       Build
disabled_fork       Check
active              Pre-release
active              Release
active              CI
```

### ⚠️ Habilitar Actions NO basta para el workflow de precios

GitHub trata los workflows con disparador `schedule` de forma **independiente**: aunque hayas
pulsado el botón, *Build* y *Check* siguen en `disabled_fork`. Hay que habilitar *Build* aparte.

Y hay una trampa: `gh workflow enable "Build"` falla con *"could not find any workflows named
Build"*, porque ese subcomando solo ve los workflows activos. Hay que ir por la API con el ID:

```bash
R=BetinMcFly/google-cloud-pricing-cost-calculator
ID=$(gh api repos/$R/actions/workflows --jq '.workflows[] | select(.name=="Build") | .id')
gh api -X PUT repos/$R/actions/workflows/$ID/enable
```

Comprueba que *Build* pasa a `active` repitiendo el `gh api` de arriba.

---

## Parte C — Dar permiso de escritura a los workflows

### Por qué

El workflow *Build* (`build-pricing.yml`) termina haciendo `git commit` y `git push` del
`pricing.yml` regenerado. Con el permiso por defecto actual (`read`) ese push falla.

Comprobado: `default_workflow_permissions` está en `"read"`.

### Comando

```bash
gh api -X PUT \
  repos/BetinMcFly/google-cloud-pricing-cost-calculator/actions/permissions/workflow \
  -f default_workflow_permissions=write
```

### Equivalente en la consola web

Settings → Actions → General → *Workflow permissions* → **Read and write permissions** → Save.

### Verificar

```bash
gh api repos/BetinMcFly/google-cloud-pricing-cost-calculator/actions/permissions/workflow
```

Debe responder `"default_workflow_permissions":"write"`.

---

## Parte D — Guardar la API key como secret

### Por qué ese nombre exacto

`build-pricing.yml` ya lee `${{ secrets.API_KEY }}`. **El nombre debe ser exactamente `API_KEY`**
o el pipeline no la encontrará. No hay que modificar el workflow.

### Comando

```bash
# Se pide por stdin para que la clave NO quede en el historial del shell
gh secret set API_KEY --repo BetinMcFly/google-cloud-pricing-cost-calculator
# (pega el valor y pulsa Ctrl+D)
```

### Equivalente en la consola web

Settings → Secrets and variables → Actions → New repository secret →
Name: `API_KEY` → Secret: el valor → Add secret.

### Verificar

```bash
gh secret list --repo BetinMcFly/google-cloud-pricing-cost-calculator
```

Aparecerá `API_KEY` con su fecha. GitHub **nunca** permite volver a leer el valor: si lo pierdes,
se genera otra clave y se sobrescribe el secret.

---

## Parte E — Desactivar los workflows que no aplican al fork

### Por qué

Tres de los cinco workflows heredados no tienen sentido aquí y, si se quedan activos, generan
ruido y fallos recurrentes:

| Workflow | Problema en tu fork |
|---|---|
| **Check** (`check-changes.yml`) | Se autentica contra el proyecto GCP **de upstream** (`projects/586925744942/...`, cuenta `gh-cost-calculator@billing-api-340811`). En tu fork falla siempre. Además **abre issues automáticamente** en cada ejecución. Corre a diario. |
| **Release** (`release.yml`) | Publica releases con binarios para 6 plataformas. No los necesitas. |
| **Pre-release** (`pre-release.yml`) | Igual que el anterior. |

Se conservan **Build** (regenera los precios) y **CI** (compila y pasa los 543 asertos en cada push).

### Comandos

```bash
R=BetinMcFly/google-cloud-pricing-cost-calculator
gh workflow disable "Release"     --repo $R
gh workflow disable "Pre-release" --repo $R
```

**No hace falta tocar *Check***: al tener disparador `schedule`, GitHub ya lo dejó en
`disabled_fork` por su cuenta, que es exactamente el estado que queremos. (`gh workflow disable
"Check"` fallaría de todos modos, por la misma razón que `enable` en la Parte B.)

> Solo funciona **después** de la Parte B.

### Verificar

```bash
gh api repos/$R/actions/workflows --jq '.workflows[] | "\(.state)\t\(.name)"'
```

Estado correcto al terminar:

```
active              Build              <- regenera los precios
disabled_fork       Check              <- desactivado por GitHub, lo dejamos así
disabled_manually   Pre-release
disabled_manually   Release
active              CI                 <- valida cada push
```

---

## Parte F — Primera regeneración de precios

Ahora se prueba el pipeline completo de punta a punta.

```bash
R=BetinMcFly/google-cloud-pricing-cost-calculator
gh workflow run "Build" --repo $R      # dispara workflow_dispatch
gh run watch --repo $R                 # seguir la ejecución en vivo
```

Tarda unos minutos. Consulta miles de SKUs a la Billing API, deriva el `pricing.yml` y **solo
entonces** lo valida.

### Qué tiene que pasar

| Paso del workflow | Qué comprueba |
|---|---|
| *Export SKUs and do mapping* | La API key funciona y responde el catálogo |
| *Generate Pricing* | `pricing.pl` deriva el YAML sin errores |
| **Test** | `t/gcosts.sh` + `t/test.sh` → los ~543 asertos contra la calculadora oficial de Google |
| **Control** | `t/diffcheck.sh` → aborta si se borró más de lo que se añadió |
| *Release* | Commit `Pricing updated` a `master` |

Si el paso **Test** falla, el `pricing.yml` **no** se publica. Ese es exactamente el guardarraíl
que buscábamos: un precio mal derivado no llega a producción.

### Verificar el resultado

```bash
git fetch upstream
git log --oneline -3 origin/master     # debe aparecer un "Pricing updated" tuyo
```

---

## Resumen de responsabilidades

| Paso | Quién |
|---|---|
| A — API key en GCP | CLI (`gcloud`) |
| **B — Habilitar Actions** | **Tú, en el navegador** (no hay API) + habilitar *Build* por CLI |
| C — Permisos de escritura | CLI (`gh api`) |
| D — Guardar el secret | CLI (`gh secret set`) |
| E — Desactivar workflows | CLI (`gh workflow disable`) |
| F — Primera regeneración | CLI (`gh workflow run`) |

## Mantenimiento posterior

- **Automático:** el workflow *Build* corre los jueves a las 03:45 UTC.
- **Bajo demanda**, antes de una propuesta importante:
  `gh workflow run "Build" --repo BetinMcFly/google-cloud-pricing-cost-calculator`
- **Sincronizar con upstream** (para traer mejoras del código, no de precios):
  `git fetch upstream && git checkout master && git merge --ff-only upstream/master`
- **Rotar la API key:** crear una nueva (Parte A), actualizar el secret (Parte D), y borrar la
  anterior con `gcloud services api-keys delete <UID>`.
