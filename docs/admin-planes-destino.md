# Administración de planes por destino y markup

Diseño acordado para el catálogo de planes, la disponibilidad por destino y el markup comercial. Aplica a todas las compañías que tengan adapter de cotización.

## Objetivo

- Administrar qué planes se venden y en qué destinos (1–5).
- Guardar el markup comercial del plan en tres porcentajes (productor, organizador, gastos operativos) que se suman al cotizar.
- Dejar de prender/apagar compañías comentando código: se usa `companies.active`.

## Modelo

### `companies`

Seed de las compañías con adapter. El admin **lista y prende/apaga**. No hay alta libre (sin adapter, una compañía nueva no cotiza).

| Campo | Uso |
| --- | --- |
| `slug` | Clave estable que engancha el provider (`terrawind`, `cardinal`, …). Unique. |
| `name` | Nombre de display; coincide con `company_name` del provider. |
| `active` | Si es `false`, no se llama al provider. |

Seed inicial:

| slug | name | active |
| --- | --- | --- |
| `pax` | Pax | `false` |
| `cardinal` | Cardinal | `true` |
| `go_assistance` | GoAssistance | `true` |
| `terrawind` | Terrawind | `true` |
| `new_travel` | New Travel | `true` |
| `inter_assist` | Inter Assist | `true` |
| `universal` | Universal | `true` |
| `omint` | Omint | `true` |

El seed es idempotente por `slug`: no pisa un `active` que el admin ya haya cambiado.

### `plans`

Identidad comercial del producto.

| Campo | Uso |
| --- | --- |
| `company_id` | FK a `companies`. |
| `external_plan_id` | ID del plan en la API de la compañía (ej. `product_id` de SETW). |
| `name` | Nombre local, carga manual. |
| `producer_markup` | Markup productor (%). Default `0`. |
| `organizer_markup` | Markup organizador (%). Default `0`. |
| `operating_expenses` | Gastos operativos (%). Default `0`. |
| `active` | On/off global. Baja lógica: `active=false`. Si es `false`, no se cotiza en ningún destino. |

Unique: `(company_id, external_plan_id)`.

Si un plan está inactivo, no se puede crear otro con el mismo ID: hay que reactivarlo.

### `plan_destinations`

Disponibilidad plan × destino.

| Campo | Uso |
| --- | --- |
| `plan_id` | FK a `plans`. |
| `destination_id` | `1` Nacional, `2` Latinoamérica, `3` Europa, `4` Resto del mundo, `5` Norteamérica. |
| `enabled` | Si se cotiza ese plan en ese destino. |

Unique: `(plan_id, destination_id)`.

Al crear un plan se insertan las 5 filas con `enabled=false` (opt-in).

No hay disponibilidad por tipo de viaje (único / multiviaje / larga estadía) en esta versión.

## Admin (solo rol `ADMIN`)

### Compañías — `GET/PATCH /v1/companies`

- Listar (incluye inactivas, para poder reactivarlas).
- Toggle `active`.
- Sin POST ni DELETE.

### Planes — `/v1/plans`

- Alta manual: `company_id`, `external_plan_id`, `name` y los tres markups (default `0`). Destinos nacen apagados.
- Listado y detalle devuelven los tres campos: `producer_markup`, `organizer_markup`, `operating_expenses`.
- Listado con filtros: `company_id`, `destination_id` (solo planes con ese destino `enabled`), `active`.
- Editar nombre, los tres markups, `active`.
- Toggle por destino.
- DELETE = baja lógica (`active=false`).

## Cotización

Orden por cada provider:

1. Si no hay fila de `companies` para su `slug`, o `company.active=false` → no se llama al provider.
2. Se cotiza con el adapter.
3. Si la compañía **no tiene ningún plan** en `plans` (ni activos ni inactivos) → se devuelven los planes de la API como hoy, sin filtrar ni aplicar markup.
4. Si **tiene al menos un plan** → whitelist:
   - Solo sale un resultado si existe `plans` con ese `external_plan_id`, `plan.active=true` y el destino del request está `enabled`.
   - Un producto que la API traiga y no esté en la tabla, no se cotiza.

### Markup

Los tres porcentajes se **suman** (no se componen). El total es el que se aplica al precio y el que sale en `/v1/quotes` como `markup`. El desglose solo vive en el admin de planes.

```
markup         = producer_markup + organizer_markup + operating_expenses
factor         = 1 + markup / 100
final_rate     = final_rate * factor
final_rate_usd = final_rate_usd * factor
net_rate       no se modifica
```

No se guarda un campo extra `tarifa_api`: ese precio **es** `final_rate` antes del markup. Después del markup, `final_rate` pasa a ser el precio de venta nuestro.

- **Terrawind:** SETW devuelve netas (`price` USD = `local_price` / `tc`). El adapter deja `net_rate = final_rate`. El markup sobre `final_rate` es markup sobre el neto.
- **Otras:** `final_rate` ya es el PVP de la API; un 5–10% se suma a ese PVP.
- **Markup 0:** `factor = 1`, el PVP no cambia.

## Terrawind

- Queda **prendido** (`companies.active=true` y el provider en el orquestador).
- Hasta que exista el primer plan de Terrawind en la tabla, cotiza todos los productos de SETW (regla 3).
- Al cargar el primer plan, pasa a whitelist. Si los destinos siguen apagados, deja de devolver planes hasta habilitarlos.
- `price` / `local_price` son netas (`comission_price` siempre 0). No se resta comisión.
- `final_rate` = `local_price`, `final_rate_usd` = `price`, `net_rate` = `final_rate`. El catálogo incrementa `final_rate` con el markup.

## Fuera de alcance

- Sync de planes desde las APIs de las compañías.
- Markup distinto por destino.
- Disponibilidad por tipo de viaje.
- CRUD para crear compañías nuevas.
- Comisión de agencia/vendedor: no se mezcla con el markup del plan.

## Endpoints

| Método | Path | Descripción |
| --- | --- | --- |
| `GET` | `/v1/companies` | Lista compañías (seed). Query: `active`. |
| `PATCH` | `/v1/companies/{id}` | Body: `{ "active": true \| false }`. |
| `POST` | `/v1/plans` | Alta de plan. Destinos en `false`. |
| `GET` | `/v1/plans` | Listado. Query: `company_id`, `destination_id`, `active`, `limit`, `offset`. |
| `GET` | `/v1/plans/{id}` | Detalle (incluye inactivos, para reactivar). |
| `PATCH` | `/v1/plans/{id}` | `name`, `producer_markup`, `organizer_markup`, `operating_expenses`, `active`, `destinations`. |
## Aplicación

- En desarrollo (SQLite + `create_all`): al levantar uvicorn se crean las tablas y se hace seed de compañías.
- En entornos con Alembic: `alembic upgrade head` (revisión `9f3c1d8a4e21`). El seed de compañías también corre al arrancar, sin pisar `active` ya guardado. La migración copia el `markup` anterior a `producer_markup`.

Ejemplos de requests: `tests/admin-planes.http`.
