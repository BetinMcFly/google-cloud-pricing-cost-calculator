# inventario — de un CSV de inventario a un CSV de costes

Subes un inventario en CSV y sale el coste mensual de GCP, recurso por recurso.

```bash
# Si el CSV ya viene en el esquema canonico
bash calcular.sh mi-inventario.csv nombre-del-caso us-central1

# Si es el CSV tal como lo entrego el cliente
bash calcular.sh suyo.csv nombre-del-caso us-central1 perfiles/cliente.yml
```

Eso deja `<entrada>-costes.csv` al lado del fichero de entrada, y por pantalla el
total desglosado.

---

## Por qué hay dos motores de precio

Ninguno cubre todo, y conviene tenerlo claro antes de leer un total:

| Motor | Qué precia | De dónde salen los precios |
|---|---|---|
| **gcosts** | máquinas, discos, buckets, VPN, NAT, monitoring, egress | `pricing.yml` horneado en la imagen, validado con 543 asertos contra la calculadora oficial de Google |
| **tarifas** | BigQuery, Cloud Logging, Pub/Sub, Bigtable, Dataplex… | `tarifas.yml`, precio unitario leído de la Cloud Billing Catalog API y anclado con fecha |

El reparto lo decide la columna `tipo` de cada fila. Los tipos de gcosts están en
`TIPOS_GCOSTS` dentro de `inventario.py`; los demás se definen en `servicios.csv`.

**Los dos motores tienen garantías distintas.** Lo que precia gcosts está
contrastado renglón a renglón con la calculadora oficial. Lo que precia `tarifas`
es el precio de lista del SKU multiplicado por la cantidad que tú declares: el
precio es correcto, pero **la cantidad es un supuesto tuyo**, y en servicios de
consumo (TiB escaneados, DCU-hora) ese supuesto pesa más que el precio. La columna
`motor` del CSV de salida dice de cuál viene cada línea, para que no se mezclen al
leerlos.

---

## Cada cliente entrega otro formato

Es la norma, no la excepcion: otras columnas, otras unidades, otro vocabulario.
En vez de adivinar, cada cliente tiene un **perfil declarativo** en `perfiles/`
que dice como leer lo suyo. El perfil se guarda junto a la propuesta y es lo que
permite rehacer el mismo calculo un ano despues.

### 1. Mirar que trae

```bash
python3 inventario.py inspeccionar --csv suyo.csv --borrador perfiles/cliente.yml
```

Detecta el delimitador, y por cada columna enseña cuantos valores distintos trae,
cuantos vienen vacios, una muestra, y para que **podria** servir:

```
COLUMNA                          DISTINTOS  VACIOS  MUESTRA / PISTA
VM Name                                  7       0  SRV-ERP-01 | SRV-ERP-02   <- podria ser nombre
CPUs                                     5       0  8 | 4 | 16                <- podria ser vcpu
Memory MB                                5       0  32768 | 16384             <- podria ser ram
```

Con `--borrador` escribe un perfil de partida. **Es una conjetura a partir del
nombre de la cabecera**, no una lectura del contenido: hay que repasarlo.

### 2. Corregir lo que el borrador no puede saber

Tres cosas no estan en el nombre de la columna, y las tres cambian el resultado:

| Qué | Por qué importa |
|---|---|
| **Las unidades reales** | RVTools rotula `Memory MB` pero entrega **MiB**. Leerlo como MB decimal infla la RAM un 4,8% y salta al siguiente tipo de maquina, mas caro |
| **Qué filas no se calculan** | Una VM apagada no se migra. Si el cliente quiere contarla igual, se quita el filtro y se declara como supuesto |
| **Qué vale cada valor del cliente** | `Red Hat Enterprise Linux 8` → `rhel`; `Ubuntu 22.04` → sin licencia |

### 3. Calcular

```bash
bash calcular.sh suyo.csv cliente-x us-central1 perfiles/cliente.yml
```

### Anatomía de un perfil

Ejemplo completo y comentado en `perfiles/ejemplo-vmware.yml`.

```yaml
csv:
  delimitador: ';'
  decimal: '.'          # ',' si usa coma decimal
  saltar_lineas: 0      # cabeceras de adorno antes de la tabla

filtrar:                # las filas que no pasan no se calculan
  - columna: Powerstate
    excluir: ['poweredoff', 'suspended']

salidas:                # UNA fila del cliente puede dar VARIAS canonicas
  - tipo: {constante: vm}
    nombre: {desde: 'VM Name'}
    spec:
      sizing:           # no hay tipo de maquina: se deduce de vCPU + RAM
        vcpu: 'CPUs'
        ram_gb: 'Memory MB'
        ram_unidad: MiB
    so:
      desde: 'OS'
      mapa:
        '*red hat*': rhel
        '*windows*': windows
        '*': ''         # cajon de sastre: sin licencia

  - tipo: {constante: disco}
    nombre: {desde: 'VM Name', sufijo: '-disco'}
    cantidad: {desde: 'Provisioned MiB', unidad: {de: MiB, a: GiB}}
    padre: {desde: 'VM Name'}
    omitir_si_vacio: cantidad   # sin disco, no se genera la fila
```

Cada campo sale de una de tres formas: `constante`, `desde` (una columna o
**una lista de columnas**) o `sizing`. Sobre eso se aplican, en orden, `mapa`
(traduccion por patrones), `unidad` (conversion) y `prefijo`/`sufijo`.

### Nombres que ninguna columna da por si sola

`desde` acepta una lista, y entonces concatena. Es lo normal cuando el cliente
organiza el inventario por aplicacion y no por maquina: cinco filas se llaman
`APIS-SIEL` y son cinco servidores distintos.

```yaml
nombre: {desde: ['Aplicación / Servidor', '# VMs'], separador: ' @ '}
```

Las partes vacias no dejan separadores sueltos: una fila sin la segunda columna
conserva la primera como nombre.

Importa porque **el nombre es la clave**: es lo que une un disco con su vm y lo
que identifica la linea en la propuesta. Dos recursos del mismo tipo con el
mismo nombre **abortan** el calculo, en vez de producir un CSV donde no se sabe
que disco cuelga de que maquina.

### Celdas que dicen "no hay dato" sin estar vacias

Casi ningun inventario real deja la celda en blanco: escribe `-`, `N/A` o `n/d`.
Sin declararlo, ese texto llega al conversor numerico y aborta el calculo.

```yaml
csv:
  vacios: ['-', 'N/A']
```

Declarado, la celda se comporta como vacia: la cazan los filtros y funcionan los
`omitir_si_vacio`.

### El dimensionado desde vCPU y RAM

Muchos inventarios on-prem no traen tipo de maquina. `sizing` elige **el mas
barato de GCP que cabe**, con dos reglas que evitan errores caros:

1. **Nunca se queda corto.** Se exige `vCPU >= origen` y `RAM >= origen`.
2. **Solo familias de proposito general y computo, y solo x86** (e2, n1, n2,
   n2d, n4, c2, c2d, c3, c3d, c4, c4d, t2d). Dos exclusiones, por motivos
   distintos:
   - Una maquina con GPU de 12 vCPU cabe en una carga de 12 vCPU, pero cuesta
     un orden de magnitud mas.
   - `t2a` (Ampere Altra) y `c4a` (Axion) son **Arm**. Suelen ganar por precio,
     pero una carga x86 no se mueve ahi sin recompilar y sin comprobar que
     existan los paquetes. Proponer Arm como si fuera un lift-and-shift
     compromete un trabajo de migracion que nadie ha presupuestado.

   Para usar cualquiera de ellas hay que pedir la familia explicitamente con
   `familia:`.

El desempate es **por precio real de `pricing.yml` en la region**, no por orden
alfabetico: `c2-standard-8` y `n2-standard-8` tienen los mismos 8 vCPU y 32 GiB
y se llevan 17 USD/mes; `e2-standard-8` es 48 USD/mes mas barata que la c2. Un
tipo sin precio en esa region se descarta: no existe alli.

El tipo elegido sale en el CSV normalizado, para poder discutirlo con el cliente
antes de calcular nada.

---

## El fichero de inventario

Un CSV, una fila por recurso. Las líneas que empiezan por `#` se ignoran.

| Columna | Obligatoria | Para qué |
|---|---|---|
| `tipo` | sí | Decide el motor y cómo se leen las demás columnas |
| `nombre` | sí | Identificador libre; sale tal cual en el CSV de costes |
| `region` | no | Si se deja vacía, se usa la región por defecto del comando |
| `spec` | según tipo | vm: tipo de máquina · disco: `ssd`/`hdd`/`balanced` · bucket: `standard`/`nearline`/… |
| `cantidad` | según tipo | GiB, TiB, nodos… |
| `unidad` | no | Solo documental, para quien lea el CSV |
| `so` | no | Solo vm. **Únicamente** `rhel`, `rhel-sap`, `sles`, `sles-sap`, `windows` |
| `compromiso` | no | Solo vm: `0`, `1` o `3` años de CUD |
| `padre` | no | Solo disco: nombre de la vm de la que cuelga |
| `notas` | no | Texto libre |

Hay un ejemplo completo y comentado en `ejemplo-inventario.csv`.

### La columna `so` tiene una trampa

`so` es la **licencia que se cobra**, no el sistema operativo. Debian, Ubuntu,
CentOS y demás no llevan licencia: la columna se deja **vacía**. Poner `free`, o el
nombre de una distribución sin licencia, aborta el cálculo entero en gcosts con
`License 'free' for GCE machine type ... not found!`. El conversor lo valida antes
de subir nada, así que el error sale en tu máquina y no gasta una ejecución.

### Tipos disponibles

De gcosts: `vm`, `disco`, `bucket`, `egress`, `vpn`, `nat`, `monitoring`.

De `servicios.csv`: `bigquery-almacenamiento-activo`, `bigquery-almacenamiento-largo`,
`bigquery-almacenamiento-fisico`, `bigquery-consulta`, `logging-ingesta`,
`logging-retencion`, `pubsub-entrega`, `bigtable-nodo`, `dataplex-procesamiento`,
`dataplex-metadatos`.

Un tipo que no esté en ninguna lista **aborta** y te enseña los válidos. Nunca se
ignora una fila en silencio: una fila perdida es un coste que no aparece en la
propuesta.

---

## Añadir un servicio nuevo

No hace falta tocar código. Se añade una línea a `servicios.csv`:

```
tipo,servicio_id,servicio,sku_patron,unidad_esperada,periodo,notas
```

- `servicio_id` — el del catálogo. Para encontrarlo:
  ```bash
  gcloud billing services list --filter="displayName~Spanner" --format="value(serviceId,displayName)"
  ```
- `sku_patron` — regex contra la descripción del SKU, aplicado **después** de
  filtrar por región. `{region}` se sustituye por la región pedida.
- `periodo` — el campo que más se presta a error:

| periodo | La cantidad es… | Factor |
|---|---|---|
| `mes` | un stock mensual (GiB almacenados) | ×1 |
| `hora-permanente` | algo encendido todo el mes (nodos) | ×730 |
| `consumo` | el consumo del mes en la unidad del SKU (TiB escaneados) | ×1 |

**Entre `hora-permanente` y `consumo` hay un factor 730.** Un nodo de Bigtable
está encendido siempre; un escaneo de Dataplex va a ráfagas. Elegir mal no da un
aviso: da una factura equivocada.

Luego se regenera el catálogo:

```bash
python3 inventario.py tarifas --regiones us-central1,us-east1 -o tarifas.yml
```

Si un patrón casa con **cero** SKUs o con **más de uno**, el programa aborta y no
escribe nada. Nunca elige uno: un SKU equivocado da un precio plausible y falso,
que es el peor resultado posible.

---

## Flujo completo, paso a paso

`calcular.sh` hace esto por ti. Se detalla por si hay que intervenir en medio.

```bash
# 1. Catálogo de tarifas. Solo cuando cambien los servicios o quieras precios
#    frescos: es lo que ancla la propuesta a una fecha.
python3 inventario.py tarifas --regiones us-central1 -o tarifas.yml

# 2. Inventario -> YAML de uso de gcosts
python3 inventario.py convertir --csv mi-inventario.csv --dir caso/ \
    --region us-central1 --proyecto cliente-x

# 3. Al bucket. El directorio debe contener SOLO ficheros de uso: 'calc' parsea
#    todos los *.yml que encuentre y aborta si tropieza con la lista de precios.
gcloud storage cp caso/01-inventario.yml \
    gs://gcosts-casos-claude-projects-496723/casos/cliente-x/

# 4. El Job
gcloud run jobs execute gcosts --region=us-central1 --wait \
    --args=calc,--dir=/mnt/casos/casos/cliente-x,--pricing=/pricing.yml,--csv=/mnt/casos/casos/cliente-x/costs.csv

# 5. De vuelta
gcloud storage cp gs://gcosts-casos-claude-projects-496723/casos/cliente-x/costs.csv .

# 6. Unir los dos motores
python3 inventario.py calcular --csv mi-inventario.csv --region us-central1 \
    --tarifas tarifas.yml --costes-gcosts costs.csv --salida costes.csv
```

---

## El CSV de salida

| Columna | Contenido |
|---|---|
| `motor` | `gcosts` o `tarifas`. Con qué garantía se calculó la línea |
| `categoria` | Servicio (BigQuery, Cloud Bigtable…) o recurso de gcosts (vm, disk…) |
| `region`, `nombre`, `tipo` | Del inventario |
| `cantidad`, `unidad` | Lo declarado, con la unidad del SKU |
| `precio_unitario` | Precio de lista del SKU (solo motor `tarifas`) |
| `factor_mes` | 1 o 730. Deja a la vista de dónde sale el importe mensual |
| `coste_mes` | USD/mes |
| `sku` | Identificador del SKU, para poder auditar la cifra |

`precio_unitario` y `factor_mes` están para que cualquiera pueda rehacer la
multiplicación a mano. Un número que no se puede reconstruir no debería ir en una
propuesta.

---

## Pruebas

```bash
bash test.sh
```

49 comprobaciones, sin tocar la red (usa las tarifas fijas de `t/`). Verifican
importes, la traducción de perfiles y, sobre todo, que los guardarraíles
**abortan**:

- un `so` que gcosts no cobra
- un tipo de recurso desconocido
- un disco colgado de una vm que no está en el inventario
- una cantidad que no es un número
- una columna que no existe en el esquema
- un `tarifas.yml` de una versión anterior
- un `costs.csv` que no es de gcosts
- una región sin tarifas cargadas
- un inventario con recursos de gcosts pero sin su `costs.csv`
- un perfil que apunta a una columna que no existe
- un valor del cliente que no casa con ningún patrón del mapa
- un perfil con un campo que no es canónico
- un `sizing` para el que ninguna máquina llega
- dos recursos del mismo tipo con el mismo nombre
- un `-` sin declarar en `vacios`, tanto en el sizing como en una cantidad
- que el sizing por defecto **no** elija Arm, y que sí lo elija cuando se pide

Ese segundo bloque es el que importa. El fallo peligroso de una calculadora no es
el que se cae: es el que devuelve un número creíble y equivocado.

---

## Lo que estos números NO son

- **Precios de lista públicos.** Acuerdos empresariales y descuentos negociados no
  están en la Billing API. Para eso existe el campo `discount` en los YAML de uso
  de gcosts (ver `usage/README.md`).
- **BigQuery on-demand.** `bigquery-consulta` precia TiB escaneados. Si hay
  reservas de slots, ese SKU no aplica y el número sobra.
- **Sin free tier.** Se ignora deliberadamente, igual que en gcosts.
- **Sin Spot.** No es un precio comprometible y no debe ir en una propuesta.
  `calcular` avisa si detecta la palabra en la columna `notas`.
- **La cantidad es tuya.** En los servicios de consumo, el supuesto de volumen pesa
  más que el precio unitario. Declararlo junto al número.
