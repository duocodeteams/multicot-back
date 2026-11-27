# Emisión Terrawind

El panel cotiza como hasta ahora y, al elegir un plan Terrawind, abre una venta. El cobro usa **Nave**. SETW solo emite el voucher después del pago aprobado (`add_vouchers`).

El precio a mostrar es `amount` en **ARS** (Nave cobra en pesos).

## Orden

1. `POST /v1/quotes`. El `id` del plan es el `product_id` SETW.
2. `POST /v1/sales` con `company: "terrawind"`, `quote_id` (= product_id) y el snapshot `quote`.
3. `GET /v1/sales/terrawind/countries` (opcional) para elegir `setw_country_id` (default `9999` = Múltiples Destinos).
4. `POST /v1/sales/{id}/passengers` — **todos** los pasajeros llevan `gender` (`m`/`f`).
5. `POST /v1/sales/{id}/payment` con `payment_method_id: "nave"`. Antes del link, el backend hace `check_voucher` en SETW. La respuesta trae `checkout_url` de Nave para enviar al pax.
6. Nave notifica `POST /v1/nave/webhooks` → si el pago está approved, se emite con `add_vouchers`.
7. Al volver al panel: `POST /v1/sales/{id}/payment/confirm` (reconsulta Nave si sigue pending; si approved, emite).
8. `GET /v1/sales/{id}` para retomar.

## Abrir venta

```json
{
  "company": "terrawind",
  "quote_id": "10432",
  "quote": {
    "departure_date": "2026-06-20",
    "return_date": "2026-06-27",
    "ages": [35],
    "origin": "AR",
    "destination_id": 3,
    "trip_type": "unico_viaje",
    "amount": "177.00",
    "currency": "ARS",
    "plan_name": "TW PLAN DEMO",
    "markup": "0",
    "setw_country_id": "9999"
  }
}
```

## Pasajeros

Mismo contrato base que Omint/Inter Assist, más `gender` obligatorio (`m` o `f`).

`document_type`: `dni` → 1, `passport` → 2, `cuit` → 3 en SETW.

## Pago Nave

```json
{
  "payment_method_id": "nave",
  "return_url_ok": "https://panel/.../ok",
  "return_url_error": "https://panel/.../error",
  "return_url_pending": "https://panel/.../pendiente"
}
```

No hay `/payment/simulate` para Terrawind.

Configurar en `.env`: `NAVE_*`, `NAVE_NOTIFICATION_URL` (pública → `/v1/nave/webhooks`), `NAVE_CALLBACK_URL` opcional, `TERRAWIND_*`.

## Voucher PDF

SETW expone un link público con `voucher_number` + `voucher_key`. Queda en `voucher_url` de confirm/`GET /v1/sales/{id}`.

Sandbox: `https://sandbox.setw.net/emision/voucher.php?...`

## Estados

Emisión: `product_selected`, `passengers_complete`, `emitting`, `issued`, `emission_failed`.

Pago: `not_started`, `pending`, `approved`, `rejected`.
