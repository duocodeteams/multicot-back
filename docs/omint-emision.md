# Emisión Omint

El panel cotiza como hasta ahora y, cuando el usuario elige un plan de Omint, abre una venta. Todas las rutas piden el mismo login, salvo la notificación de pago, que la llama Omint.

El precio a mostrar es `amount`. El markup de esta venta es 0: es el precio que cobra Omint.

Los campos de cada respuesta también están en `/docs`.

## Orden

1. `POST /v1/quotes`. El `id` del plan de Omint es `quotationId|productId`.
2. `POST /v1/sales` con `{ "company": "omint", "quote_id": "<ese id>" }`.
   - 201 si la venta es nueva. Queda en `emission_status: product_selected` y `payment_status: not_started`.
   - 200 si el mismo usuario ya había elegido ese producto: se devuelve la venta existente.
3. `GET /v1/sales/omint/countries` y `GET /v1/sales/omint/countries/{country_id}/provinces`. Cada ítem es `{ "id", "name" }`. Esos `id` se mandan en el titular.
4. `POST /v1/sales/{id}/passengers`.
5. `POST /v1/sales/{id}/payment`. La respuesta trae `checkout_url`. El pasajero paga en esa página.
6. Al volver al panel, `POST /v1/sales/{id}/payment/confirm`. Esa respuesta dice si el voucher existe. Volver a la URL de ok no alcanza.
7. `GET /v1/sales/{id}` sirve para retomar la pantalla en cualquier paso.

## Pasajeros

Un titular (`is_holder: true`), el resto de los viajeros y un contacto de emergencia para toda la venta.

El titular tiene que traer `email`, `phone`, `country_id`, `province_id`, `street`, `street_number` y `city`.

`document_type` es `dni`, `passport` o `cuit`. No se repiten números de documento.

La cantidad de pasajeros y las edades, calculadas a `departure_date`, tienen que coincidir con `quoted_ages`.

```json
{
  "receive_promotions": false,
  "emergency_contact": {
    "first_name": "Luis",
    "last_name": "Perez",
    "phone": "+5491100000001",
    "email": "luis.perez@example.com"
  },
  "passengers": [
    {
      "is_holder": true,
      "first_name": "Ana",
      "last_name": "Perez",
      "birth_date": "1991-04-22",
      "document_type": "dni",
      "document_number": "30111222",
      "email": "ana.perez@example.com",
      "phone": "+5491100000000",
      "country_id": "<id de GET /omint/countries>",
      "province_id": "<id de GET /omint/countries/{country_id}/provinces>",
      "country": "Argentina",
      "province": "Buenos Aires",
      "street": "Av. Ejemplo",
      "street_number": "123",
      "city": "Buenos Aires"
    }
  ]
}
```

La respuesta incluye `payment_methods`:

- `id`: se manda al abrir el pago.
- `code`
- `requires_financing`: si es true, hay que mandar `financing_id`.
- `financing`: `{ "id", "dues" }`.

## Pago

`POST /v1/sales/{id}/payment`

```json
{
  "payment_method_id": "...",
  "financing_id": "...",
  "return_url_ok": "https://panel/.../ok",
  "return_url_error": "https://panel/.../error",
  "return_url_pending": "https://panel/.../pendiente"
}
```

`financing_id` se omite cuando `requires_financing` es false. Las tres URLs tienen que ser `http` o `https`.

La respuesta trae `checkout_url` y `created`. Si ya había un checkout pendiente, `created` es false y la URL es la misma. No se abre otro cobro.

`POST /v1/sales/{id}/payment/confirm` se puede repetir.

- `payment_received: true`: vienen `voucher_number` y `voucher_url`. La venta queda `issued` y el pago `approved`.
- `payment_received: false`: sigue en `pending`. No hay que volver a llamar a `POST /payment`.

## Retomar

`GET /v1/sales/{id}` devuelve la venta, los pasajeros, el contacto de emergencia, los medios de pago, `checkout_url` si el pago está pendiente, y `voucher_number` con `voucher_url` si ya se emitió. Otro usuario recibe 403.

## Estados

Emisión: `product_selected`, `passengers_complete`, `issued`, `emission_failed`.

Pago: `not_started`, `pending`, `approved`.

Estos llamados usan Omint de producción. Si abrir el pago responde 400 por `PUBLIC_API_BASE_URL`, falta esa variable en el servidor. No la setea el panel.
