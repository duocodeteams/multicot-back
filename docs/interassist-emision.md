# Emisión Inter Assist

El panel cotiza como hasta ahora y, cuando el usuario elige un plan de Inter Assist, abre una venta. El cobro usa **nuestra pasarela** (hoy simulada). Inter Assist solo emite el voucher después del pago aprobado.

El precio a mostrar es `amount` (el de la cotización, con markup si aplica).

Los campos de cada respuesta también están en `/docs`.

## Orden

1. `POST /v1/quotes`. El `id` del plan de Inter Assist es el `plan_id` numérico.
2. `POST /v1/sales` con `company: "inter_assist"`, `quote_id` (= plan id) y el snapshot `quote` del viaje.
   - 201 si la venta es nueva. Queda en `emission_status: product_selected` y `payment_status: not_started`.
   - 200 si el mismo usuario ya había abierto la misma cotización: se devuelve la venta existente (también si ya avanzó de paso; usar `emission_status` / `GET /v1/sales/{id}` para retomar).
3. `POST /v1/sales/{id}/passengers`.
4. `POST /v1/sales/{id}/payment`. Abre el cobro simulado (`payment_status: pending`).
5. `POST /v1/sales/{id}/payment/simulate` con `{ "result": "pending" | "approved" | "rejected" }`.
6. `POST /v1/sales/{id}/payment/confirm`.
   - Si el pago sigue `pending`: `payment_received: false`.
   - Si está `approved`: llama a Inter Assist (`emitir-externa`), guarda voucher y deja la venta `issued`.
   - Si está `rejected`: 409.
7. `GET /v1/sales/{id}` sirve para retomar la pantalla en cualquier paso.

## Abrir venta

```json
{
  "company": "inter_assist",
  "quote_id": "802",
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
    "plan_name": "INTER 100",
    "promocion_id": 0,
    "markup": "0"
  }
}
```

- `unico_viaje` / `larga_estadia`: requieren `return_date`.
- `multiviaje`: requiere `days_range` 30, 60 o 90.
- `promocion_id: 0` fuerza emisión sin promo. Si se omite en Inter Assist, ellos pueden elegir una promo; acá el campo siempre se manda (default 0).

## Pasajeros

Mismo contrato que Omint: un titular, viajeros y contacto de emergencia.

La cantidad de pasajeros y las edades a `departure_date` tienen que coincidir con `quoted_ages`.

Catálogos para el formulario (no van a Inter Assist; solo `country_id` / `province_id` locales):

- `GET /v1/sales/inter_assist/countries` → REST Countries (`id` = ISO cca2, ej. `AR`).
- `GET /v1/sales/inter_assist/countries/{country_id}/provinces` → Georef si es `AR`; vacío en otros países.

Inter Assist identifica viajeros por DNI (`document_number`). El contacto de emergencia y la dirección del titular se guardan en nuestro sistema; no se envían a `emitir-externa`.

La respuesta de pasajeros incluye un medio de pago simulado:

```json
{ "id": "simulated", "code": "SIMULATED", "requires_financing": false, "financing": [] }
```

## Pago simulado

`POST /v1/sales/{id}/payment`

```json
{
  "payment_method_id": "simulated",
  "return_url_ok": "https://panel/.../ok",
  "return_url_error": "https://panel/.../error",
  "return_url_pending": "https://panel/.../pendiente"
}
```

`payment_method_id` puede omitirse (default `simulated`). Las tres URLs tienen que ser `http` o `https`. La respuesta trae `checkout_url` (por ahora la URL pending/ok del panel) y `created`.

`POST /v1/sales/{id}/payment/simulate`

```json
{ "result": "approved" }
```

Valores: `pending`, `approved`, `rejected`.

`POST /v1/sales/{id}/payment/confirm` se puede repetir.

- `payment_received: true`: vienen `voucher_number` y `voucher_url`. La venta queda `issued` y el pago `approved`.
- `payment_received: false`: el pago sigue `pending`. No hay que volver a llamar a `POST /payment`.

## PDF del voucher

La URL de Inter Assist (`GET /api/vouchers/{id}/pdf/{lang}`) **exige API Key** y no debe abrirse en el navegador (401 Unauthorized).

Usá nuestro proxy (con el JWT del panel):

`GET /v1/sales/{id}/voucher/pdf?lang=es`

- `lang`: `es` | `en` | `pt` (default `es`).
- Respuesta: `application/pdf`.
- `voucher_url` en confirm/`GET /v1/sales/{id}` apunta a esa ruta (no a Inter Assist).

En el front: `fetch` con `Authorization: Bearer …`, crear un blob y abrirlo. Un `window.open(voucher_url)` sin header falla.

La API Key necesita el scope **vouchers**. Si el proxy responde 502 con mensaje de 403, pedí ese módulo en el portal de Inter Assist.

## Retomar

`GET /v1/sales/{id}` devuelve la venta, los pasajeros, el contacto de emergencia, los medios de pago, `checkout_url` si el pago está pendiente, y `voucher_number` con `voucher_url` si ya se emitió. Otro usuario recibe 403.

## Estados

Emisión: `product_selected`, `passengers_complete`, `emitting`, `issued`, `emission_failed`.

Pago: `not_started`, `pending`, `approved`, `rejected`.

## Notas

- Emisión externa: `POST {INTERASSIST_BASE_URL}/api/ventas/emitir-externa` con la API Key del servidor.
- No usar `POST /api/ventas` (pide IDs de cliente internos).
- Requiere `INTERASSIST_API_KEY` y `INTERASSIST_BASE_URL` en el entorno.
