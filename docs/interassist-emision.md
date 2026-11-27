# Emisión Inter Assist

El panel cotiza como hasta ahora y, cuando el usuario elige un plan de Inter Assist, abre una venta. El cobro lo hace Inter Assist en su cuenta de **Mercado Pago** o **Payway**. La venta y el voucher aparecen recién cuando el pago queda aprobado.

El precio a mostrar es `amount` (el de la cotización, con markup si aplica).

Los campos de cada respuesta también están en `/docs`.

## Orden

1. `POST /v1/quotes`. El `id` del plan de Inter Assist es el `plan_id` numérico.
2. `POST /v1/sales` con `company: "inter_assist"`, `quote_id` (= plan id) y el snapshot `quote` del viaje.
   - 201 si la venta es nueva. Queda en `emission_status: product_selected` y `payment_status: not_started`.
   - 200 si el mismo usuario ya había abierto la misma cotización: se devuelve la venta existente (también si ya avanzó de paso; usar `emission_status` / `GET /v1/sales/{id}` para retomar).
3. `POST /v1/sales/{id}/passengers`. La respuesta trae las dos opciones de pago.
4. `POST /v1/sales/{id}/payment` con `payment_method_id` `mercadopago` o `payway`. El backend abre el checkout de Inter Assist y devuelve `checkout_url`.
5. El pasajero paga en esa URL. Al volver al panel: `POST /v1/sales/{id}/payment/confirm`.
   - Si la orden sigue `pending`: `payment_received: false`.
   - Si está `approved` y Inter Assist ya emitió: guarda el voucher y deja la venta `issued`.
   - Si está `rejected` o `cancelled`: 409.
6. `GET /v1/sales/{id}` sirve para retomar la pantalla en cualquier paso.

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
- `promocion_id` (obligatorio en el snapshot; default `0`):
  - Copiar **tal cual** `plans[].promocion_id` de `POST /v1/quotes`.
  - `> 0`: Inter aplica esa promo en el checkout (doc §5.4).
  - `0`: fuerza cobro **sin** promoción, aunque el plan tenga promos (doc §7.5).
  - El backend **no** adivina ni reescribe el id. Si el precio es el de promo y mandás `0`, la venta se rechaza con 400.
  - Solo se cotizan promos de descuento **% plano** (tipo Porcentaje). 2x1 / grupo no bajan el precio en carta con 1 pax.

## Pasajeros

Mismo contrato que Omint: un titular, viajeros y contacto de emergencia.

La cantidad de pasajeros y las edades a `departure_date` tienen que coincidir con `quoted_ages`.

Catálogos para el formulario (no van a Inter Assist; solo `country_id` / `province_id` locales):

- `GET /v1/sales/inter_assist/countries` → REST Countries (`id` = ISO cca2, ej. `AR`).
- `GET /v1/sales/inter_assist/countries/{country_id}/provinces` → Georef si es `AR`; vacío en otros países.

Inter Assist identifica viajeros por DNI (`document_number`). El contacto de emergencia y la dirección del titular se guardan en nuestro sistema; no se envían al checkout.

Al menos un viajero tiene que tener un email real. Sin eso el checkout responde 400.

La respuesta de pasajeros incluye las dos pasarelas:

```json
[
  {
    "id": "mercadopago",
    "code": "MERCADOPAGO",
    "name": "Mercado Pago",
    "requires_financing": false,
    "financing": [],
    "cards": [],
    "max_installments": null
  },
  {
    "id": "payway",
    "code": "PAYWAY",
    "name": "Payway",
    "requires_financing": true,
    "cards": [{ "id": "1", "name": "Visa" }],
    "max_installments": 6,
    "financing": [
      { "id": "1", "dues": 1 },
      { "id": "3", "dues": 3 }
    ]
  }
]
```

En Mercado Pago el pasajero elige las cuotas en la pasarela. En Payway hay que mandar la tarjeta (`cards[].id`) y las cuotas (`financing[].id`, de 1 a `max_installments`).

Si el listado de tarjetas no responde, la respuesta trae solo Mercado Pago. `GET /v1/sales/{id}` vuelve a pedirlo.

## Pago

`POST /v1/sales/{id}/payment`

El backend arma las URLs de retorno con `PORTAL_BASE_URL` (prueba: `https://biantravel.com`). El front no las manda:

- `{PORTAL_BASE_URL}/portal/pago?sale_id={id}&payment=ok`
- `{PORTAL_BASE_URL}/portal/pago?sale_id={id}&payment=error`
- `{PORTAL_BASE_URL}/portal/pago?sale_id={id}&payment=pending`

Mercado Pago acepta localhost. Payway no: Decidir responde 502 si la vuelta no es un `https` público.

Mercado Pago:

```json
{
  "payment_method_id": "mercadopago"
}
```

Payway:

```json
{
  "payment_method_id": "payway",
  "credit_card_id": "1",
  "financing_id": "3"
}
```

La respuesta trae el cobro en pesos que calculó Inter:

```json
{
  "checkout_url": "https://...",
  "created": true,
  "amount": "79000.00",
  "currency": "ARS",
  "exchange_rate": "1000"
}
```

`amount` / `currency` son el cobro de la pasarela (ARS). No confundir con `sale.amount` de la cotización (USD). `exchange_rate` es el `tipo_cambio` de Inter (`USD → ARS`).

Al volver al portal, esa pantalla llama a `payment/confirm`. `GET /v1/sales/{id}` también expone `payment_amount`, `payment_currency` y `exchange_rate`.

El mismo medio, con la misma tarjeta y las mismas cuotas en Payway, reutiliza el link pendiente. Si cambia la pasarela o las cuotas, se abre otro checkout.

Mercado Pago recibe las tres URLs y no recibe tarjeta ni cuotas: las elige el pasajero en la página de Mercado Pago. Payway recibe `credit_card_id` e `installments` (de 1 a `max_installments` del listado de tarjetas). En Payway `pending_url` es opcional; igual la enviamos.

`POST /v1/sales/{id}/payment/confirm` se puede repetir. Consulta la orden en Inter Assist. No llama a `emitir-externa`: esa ruta crea la venta sin cobrar.

- `payment_received: true`: vienen `voucher_number` y `voucher_url`. La venta queda `issued` y el pago `approved`.
- `payment_received: false`: el pago sigue `pending`. No hay que volver a llamar a `POST /payment`.
- Pago `rejected` o `cancelled`: 409.

Si `PUBLIC_API_BASE_URL` está definida, el alta del checkout manda `notification_url` a `POST /v1/sales/{id}/interassist/payment-notification`. Ese aviso no exige login. Igual se confirma la orden con el GET de Inter Assist. Si el aviso no llega, alcanza con `payment/confirm` al volver al panel.

## PDF del voucher

La URL de Inter Assist (`GET /api/vouchers/{id}/pdf/{lang}`) **exige API Key** y no debe abrirse en el navegador (401 Unauthorized).

Según la doc Inter (§7.4 / §7.7 / §7.11):

1. Tras el pago aprobado, la orden trae `venta.id` + `venta.voucher_ids` (un id **por viajero**).
2. Hay que **persistir cada id**.
3. El PDF de cada uno es `GET /api/vouchers/{id}/pdf/{lang}` (nosotros lo proxificamos; exige API Key).
4. El detalle `GET /api/ventas/{venta.id}` trae `vouchers[].cliente` (nombre/DNI) para saber de quién es cada PDF.

En confirm / `GET /v1/sales/{id}`:

```json
{
  "voucher_number": "90001",
  "voucher_url": "/v1/sales/55/voucher/pdf?voucher_id=90001",
  "vouchers": [
    {
      "id": 90001,
      "number": "90001",
      "url": "/v1/sales/55/voucher/pdf?voucher_id=90001",
      "first_name": "María",
      "last_name": "García",
      "document_number": "30123456"
    },
    {
      "id": 90002,
      "number": "90002",
      "url": "/v1/sales/55/voucher/pdf?voucher_id=90002",
      "first_name": "Juan",
      "last_name": "Pérez",
      "document_number": "30123457"
    }
  ]
}
```

Proxies (con JWT del panel):

- `GET /v1/sales/{id}/voucher/pdf?lang=es` → primer voucher
- `GET /v1/sales/{id}/voucher/pdf?voucher_id=90002&lang=es` → uno puntual
- `GET /v1/sales/{id}/vouchers/zip?lang=es` → zip con todos

- `lang`: `es` | `en` | `pt` (default `es`).
- PDF: `application/pdf`. Zip: `application/zip`.

En el front: mostrar `vouchers[]` (nombre + DNI) y `fetch` cada `url` con `Authorization: Bearer …`. Un `window.open` sin header falla.

La API Key necesita el scope **vouchers**. Si el proxy responde 502 con mensaje de 403, pedí ese módulo en el portal de Inter Assist.

## Retomar

`GET /v1/sales/{id}` devuelve la venta, los pasajeros, el contacto de emergencia, los medios de pago (Mercado Pago y Payway), `checkout_url` si el pago está pendiente, y `voucher_number` con `voucher_url` si ya se emitió. Otro usuario recibe 403.

## Estados

Emisión: `product_selected`, `passengers_complete`, `emitting`, `issued`, `emission_failed`.

Pago: `not_started`, `pending`, `approved`, `rejected`.

## Notas

- Checkout Mercado Pago: `POST {INTERASSIST_BASE_URL}/api/ventas/checkout/mercadopago`.
- Tarjetas Payway: `GET {INTERASSIST_BASE_URL}/api/ventas/checkout/payway/tarjetas`.
- Checkout Payway: `POST {INTERASSIST_BASE_URL}/api/ventas/checkout/payway`.
- Estado de la orden: `GET {INTERASSIST_BASE_URL}/api/ventas/checkout/{uuid}`. Ahí aparecen `venta.id` y `voucher_ids` cuando el pago está aprobado.
- `POST /api/ventas/emitir-externa` crea la venta sin cobrar. No se usa en este flujo.
- No usar `POST /api/ventas` (pide IDs de cliente internos).
- Requiere `INTERASSIST_API_KEY` y `INTERASSIST_BASE_URL` en el entorno.
- `PUBLIC_API_BASE_URL` es opcional: si está, Inter Assist puede avisar el pago aprobado.
