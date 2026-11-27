# Emisión New Travel

El panel cotiza como hasta ahora y, al elegir un plan New Travel, abre una venta. El cobro usa la **pasarela de New Travel** (`paymentLink`). El voucher se confirma al volver con `payment/confirm`.

El precio a mostrar es `amount` (el de la cotización).

## Orden

1. `POST /v1/quotes`. El `id` del plan New Travel es el `idPlan`.
2. `POST /v1/sales` con `company: "new_travel"`, `quote_id` (= idPlan) y el snapshot `quote`.
3. Catálogos (opcional): `GET /v1/sales/new_travel/countries`, `.../document-types`, `.../countries/{id}/provinces`.
4. `POST /v1/sales/{id}/passengers` — **todos** los pasajeros llevan `email` y `phone`.
5. `POST /v1/sales/{id}/payment` con `payment_method_id: "new_travel"`. El backend crea la orden con `waitForPayment` y genera el link. La respuesta trae `checkout_url`.
6. El pasajero paga en New Travel. Al volver al panel: `POST /v1/sales/{id}/payment/confirm`.
7. `GET /v1/sales/{id}` para retomar.

## Abrir venta

```json
{
  "company": "new_travel",
  "quote_id": "2329",
  "quote": {
    "departure_date": "2026-06-20",
    "return_date": "2026-06-27",
    "ages": [35],
    "origin": "AR",
    "destination_id": 3,
    "trip_type": "unico_viaje",
    "days_range": null,
    "amount": "120.50",
    "currency": "USD",
    "plan_name": "PLAN DEMO NTA",
    "markup": "0"
  }
}
```

- `unico_viaje` / `larga_estadia`: requieren `return_date`.
- `multiviaje`: requiere `days_range` (30/45/60/90/120 según plan) y fechas anuales (~365 días).

## Pasajeros

Mismo contrato base que Omint/Inter Assist (titular con dirección, contacto de emergencia).

Mapeo de documentos:

| Nuestro form | New Travel |
| --- | --- |
| `passport` | `PAS` |
| `dni` | `CC` |
| `cuit` | `CE` |

Catálogo del panel (`GET .../document-types`): `passport`, `dni`, `cuit` (el backend los mapea a PAS/CC/CE al emitir).

`country_id` del titular/pasajeros: ISO de `GET /new_travel/countries` (ej. `AR`). Prefijo telefónico AR = `54`.

Provincias: igual que Inter Assist. Son **solo locales** (no van a New Travel).

- `GET /v1/sales/new_travel/countries/AR/provinces` → Georef (provincias AR).
- Otros países → lista vacía (el front puede mandar un placeholder en `province_id` si el form lo exige).

## Pago

```json
{
  "payment_method_id": "new_travel",
  "return_url_ok": "https://panel/.../ok",
  "return_url_error": "https://panel/.../error",
  "return_url_pending": "https://panel/.../pendiente"
}
```

`POST /v1/sales/{id}/payment/confirm` al volver de cualquiera de las URLs:

- `payment_received: true`: vienen `voucher_number` y `voucher_url`.
  New Travel no envía la URL en `GET /orders/order`; se arma como `{host}/print/{code}`
  (PDF público, ej. `https://cacao.ilstechnik.com/print/CCD-XXXXXX`).
- `payment_received: false`: sigue `pending`. No hace falta volver a abrir `/payment`.

No hay webhook ni polling en el panel: alcanza con confirmar al volver.

## Estados

Emisión: `product_selected`, `passengers_complete`, `issued`, `emission_failed`.

Pago: `not_started`, `pending`, `approved`.

## Config

`.env`: `NEW_TRAVEL_BASE_URL`, `NEW_TRAVEL_USER`, `NEW_TRAVEL_PASSWORD`.
