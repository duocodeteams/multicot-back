# Emisión Cardinal

El panel cotiza como hasta ahora y, cuando el usuario elige un plan de Cardinal, abre una venta. El cobro usa **nuestra pasarela** (hoy simulada). Con el pago aprobado, esta API llama a `POST /emitir` de Cardinal.

Cardinal carga ese importe en la cuenta corriente del agente emisor. No hay checkout de Cardinal en este corte.

El precio de la venta es `amount`: el `amount` de `costoFinal` de la cotización. El markup de esta venta es 0. En la cotización el panel puede estar mostrando el importe en pesos (`costoFinalMonedaPais`). En la venta hay que mostrar `amount` y `currency` de la venta.

Los campos de cada respuesta también están en `/docs`.

## Orden

1. `POST /v1/quotes`. El `id` del plan de Cardinal es `cotizacionGuid|productoId`.
2. `POST /v1/sales` con `{ "company": "cardinal", "quote_id": "<ese id>" }`.
   - 201 si la venta es nueva. Queda en `emission_status: product_selected` y `payment_status: not_started`.
   - 200 si el mismo usuario ya había elegido ese producto: se devuelve la venta existente.
3. `GET /v1/sales/cardinal/countries`. Cada ítem es `{ "id", "name" }`. Ese `id` se manda en `country_id` de cada pasajero.
4. `POST /v1/sales/{id}/passengers`.
5. `POST /v1/sales/{id}/payment`. Abre el cobro simulado (`payment_status: pending`).
6. `POST /v1/sales/{id}/payment/simulate` con `{ "result": "pending" | "approved" | "rejected" }`.
7. `POST /v1/sales/{id}/payment/confirm`.
   - Si el pago sigue `pending`: `payment_received: false`.
   - Si está `approved`: llama a Cardinal, guarda un voucher por pasajero y deja la venta `issued`.
   - Si está `rejected`: 409.
8. `GET /v1/sales/{id}` sirve para retomar la pantalla en cualquier paso.

## Pasajeros

Un titular (`is_holder: true`), el resto de los viajeros y un contacto de emergencia para toda la venta. Cardinal pide el contacto en cada pasajero: esta API copia el de la venta en todos.

Cada pasajero tiene que traer `email`, `phone`, `country_id`, `city` y domicilio (`street`, y si hay `street_number` se concatena). El titular, además, sigue pidiendo `province_id`. Cardinal no usa la provincia.

`document_type` es `dni`, `passport` o `cuit`. Se traduce al id de `tiposDocumentos`. No se repiten números de documento. El número tiene como máximo 20 caracteres.

`country_id` es el id numérico de `GET /v1/sales/cardinal/countries`.

La cantidad de pasajeros y las edades, calculadas a `departure_date`, tienen que coincidir con `quoted_ages`.

La respuesta de pasajeros incluye el medio de pago simulado:

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

`payment_method_id` puede omitirse. Las tres URLs tienen que ser `http` o `https`. La respuesta trae `checkout_url` (por ahora la URL pending/ok del panel) y `created`.

`POST /v1/sales/{id}/payment/confirm` se puede repetir.

- `payment_received: true`: vienen `voucher_number`, `voucher_url` y `vouchers`. Hay un ítem por pasajero, con `number`, `url` (`linkImprimir`) y `passenger_id`. `voucher_number` y `voucher_url` repiten el primero.
- `payment_received: false`: el pago sigue `pending`. No hay que volver a llamar a `POST /payment`.

Para emitir hace falta el secreto del agente (`agente_emisor_secreto`), el de la agencia o el del servidor. Sin secreto, la venta no se puede abrir.

## Retomar

`GET /v1/sales/{id}` devuelve la venta, los pasajeros, el contacto de emergencia, el medio de pago, `checkout_url` si el pago está pendiente, y los vouchers si ya se emitió. Otro usuario recibe 403.

## Estados

Emisión: `product_selected`, `passengers_complete`, `emitting`, `issued`, `emission_failed`.

Pago: `not_started`, `pending`, `approved`, `rejected`.

## Notas

- Cotización guardada: `POST {CARDINAL_BASE_URL}/obtener_cotizacion`.
- Emisión: `POST {CARDINAL_BASE_URL}/emitir`.
- `montoCotizado` es el `amount` de `costoFinal`.
- No se envían `usuarioGuid`, `observaciones` ni `paisesDestino`.
