# inventario — de un CSV de inventario a un CSV de costes

Subes un inventario en CSV y sale el coste mensual de GCP, recurso por recurso.

```bash
bash calcular.sh mi-inventario.csv nombre-del-caso us-central1
```

Eso deja `mi-inventario-costes.csv` al lado del fichero de entrada, y por pantalla
el total desglosado.

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

29 comprobaciones, sin tocar la red (usa las tarifas fijas de `t/`). La mitad
verifica importes; la otra mitad verifica que los guardarraíles **abortan**:

- un `so` que gcosts no cobra
- un tipo de recurso desconocido
- un disco colgado de una vm que no está en el inventario
- una cantidad que no es un número
- una columna que no existe en el esquema
- un `tarifas.yml` de una versión anterior
- un `costs.csv` que no es de gcosts
- una región sin tarifas cargadas
- un inventario con recursos de gcosts pero sin su `costs.csv`

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
