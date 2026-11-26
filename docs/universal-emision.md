# Emisión Universal

El panel cotiza como hasta ahora. Cuando el usuario elige un plan de Universal, abre una venta. El cobro es la misma pasarela simulada que Inter Assist. Universal emite el voucher recién cuando el pago está aprobado.

Hay un solo voucher para toda la venta. Cada pasajero viaja dentro de esa emisión.

Los campos de cada respuesta también están en `/docs`.

## Orden

El mismo que Inter Assist:

1. `POST /v1/quotes`.
2. `POST /v1/sales`.
3. `POST /v1/sales/{id}/passengers`.
4. `POST /v1/sales/{id}/payment`.
5. `POST /v1/sales/{id}/payment/simulate` con `{ "result": "pending" | "approved" | "rejected" }`.
6. `POST /v1/sales/{id}/payment/confirm`.
7. `GET /v1/sales/{id}` para retomar.

El PDF es `GET /v1/sales/{id}/voucher/pdf?lang=es`. `lang` acepta `es`, `en` o `pt`. Hay que pedirlo con el JWT y abrirlo como blob. Un `window.open` sin header falla.

## Qué cambia respecto de Inter Assist

En la cotización, el plan de Universal trae:

- `id`: lead de esa cotización (`IdLeadOut`). Cambia en cada cotización. Es el `quote_id` de la venta. No volver a cotizar para validarlo.
- `plan_id`: producto (`IdProducto`). Va en `quote.product_id`.
- `plan_name`.
- `final_rate_usd`: precio en dólares. Va en `quote.amount_usd`.
- `final_rate`: precio local cuando Universal lo informa. Va en `quote.amount_local`.

Mandar los precios que se le mostraron al usuario, con markup incluido.

Si el origen no es Argentina, la cotización no trae planes de Universal. No hace falta ocultarlos a mano.

## Abrir venta

```json
{
  "company": "universal",
  "quote_id": "1-LEAD",
  "quote": {
    "departure_date": "2026-06-20",
    "return_date": "2026-06-27",
    "ages": [35],
    "origin": "AR",
    "destination_id": 3,
    "trip_type": "unico_viaje",
    "days_range": null,
    "plan_name": "UNIVERSAL 100",
    "product_id": "PROD-1",
    "amount_usd": "120.50",
    "amount_local": "150000.00",
    "markup": "0"
  }
}
```

- `quote_id` es `id` del plan, no `plan_id`.
- No van `amount`, `currency` ni `promocion_id`.
- `amount_usd` es obligatorio.
- `amount_local` es obligatorio si `destination_id` es `1` (Argentina). Esa venta queda en ARS y el precio emitido es `amount_local`. En cualquier otro destino la venta queda en USD y el precio emitido es `amount_usd`.
- `unico_viaje` y `larga_estadia` requieren `return_date`.
- Hasta 10 pasajeros.
- 201 si la venta es nueva (`product_selected`, pago `not_started`). 200 si el mismo usuario ya tenía esa cotización.

## Pasajeros y pago

El body de pasajeros, pago, simulación y confirmación es el de Inter Assist. El medio de pago es `simulated`.

Documentos admitidos: `dni`, `passport`, `cuit`. El CUIT se informa a Universal como Otros.

La dirección y el contacto de emergencia se guardan en nuestra venta. Universal recibe nombre, apellido, documento, nacimiento, email y residencia Argentina.

## Confirmar

- Pago `approved`: `payment_received: true`, un `voucher_number` y `voucher_url`. La venta queda `issued`.
- Pago `pending`: `payment_received: false`.
- Pago `rejected`: 409.

## Organización de la agencia

El RowId de Siebel es uno por agencia. Se carga en la ficha de la agencia, no en el vendedor. El vendedor usa el de su agencia.

`PUT /v1/agencies/{id}/credentials/universal`

```json
{ "organizacion_emisora": "1-CVOEAYQ" }
```

Si la agencia no tiene credencial, la cotización y la emisión usan `UNIVERSAL_ORGANIZACION_EMISORA`. Usuario y convenio siguen siendo los del servidor. `GET` y `DELETE` son los mismos de las credenciales de agencia.

## Estados

Emisión: `product_selected`, `passengers_complete`, `emitting`, `issued`, `emission_failed`, `cancelled`.

Pago: `not_started`, `pending`, `approved`, `rejected`.
