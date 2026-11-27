# New Travel — Plataforma de pagos y emisión de vouchers

Resumen extraído de la API Rest (misma familia Thalo Assist / New Travel Assistance).

Fuente de documentación: `https://thaloassist.page/api/v1/documentation`  
Base URL New Travel (según pruebas locales): `https://newtravelassistance.page/api/v1`

Este documento cubre solo lo necesario para:

1. Emitir vouchers pendientes de pago.
2. Cobrar con la plataforma de pagos de New Travel (link / tarjeta / pago externo).
3. Confirmar pago y entregar el voucher.

No incluye cotización, planes, precompras, SmartDelay ni reportes.

## Decisiones de integración

- Cobro: pasarela New Travel (`waitForPayment` + `paymentLink`). Sin polling en panel: al volver, un `payment/confirm` consulta una vez.
- Contacto de la orden: emergencia (fallback titular).
- `individualVoucher`: `true`.
- Prefijo AR: `54` (vía `generals/country/AR`).
- Tipos de documento expuestos al panel (pocos; el catálogo live tiene muchos):

| Código New Travel | Label | Mapeo desde nuestro form |
| --- | --- | --- |
| `PAS` | Pasaporte | `passport` |
| `CC` | Cédula / DNI | `dni` |
| `CE` | Cédula de extranjería | (opción extra; no hay equivalente fijo) |

---

## Autenticación (requerida en todo)

### POST Obtener token

`POST {base}/auth/get_token`

```json
{
  "user": "example_user",
  "password": "example_password"
}
```

Respuesta:

```json
{
  "status": "OK",
  "data": {
    "token": "L5kWN8gHNM7gm573aU",
    "language": "spa"
  }
}
```

Reglas:

- El token se envía en el header `Token` en cada request autenticado.
- Header opcional `Language` (ej. `spa` / `esp`).
- Validez: **6 minutos**.
- El token queda atado a la **IP** con la que se generó; no sirve desde otra IP.
- Errores típicos: `1000` credenciales, `1002` IP no autorizada / no coincide, `1003` sesión expirada.

Formato estándar de respuesta API:

- `status`: `OK` | `ERROR` | `UNAUTHORIZED` | `NOT_FOUND`
- `data`: payload cuando hay datos
- `error`: `{ code, message, field? }` cuando falla

Fechas en requests: formato `Y/m/d` (ej. `2026/04/20`).  
IDs y montos suelen venir serializados como **string**.

---

## Flujo recomendado (pago en plataforma New Travel)

Este es el flujo pensado para que el cliente pague en la página de New Travel y recién ahí se entregue el voucher.

1. Obtener token.
2. (Opcional) `GET orders/type_documents` para tipos de documento válidos.
3. `POST orders/create` con `waitForPayment: "Y"`.
   - Se crea la orden **pendiente de pago**.
   - `voucher` llega en `null`.
   - Se obtiene `voucherCode` (código de orden) y `total`.
4. `POST payments/paymentLink` con ese código.
   - Se obtiene `paymentLink.url` para redirigir / mostrar al cliente.
5. **Polling** (no hay webhooks): consultar la emisión y mirar `paymentLink.status`.
6. Cuando el link esté `USED`:
   - La emisión quedó activa / pagada.
   - Consultar emisión para datos definitivos (`paid`, `status`, `paymentLink`).
   - La consulta **no** trae URL de voucher: el PDF es `{host}/print/{voucherCode}`
     (ej. `https://cacao.ilstechnik.com/print/CCD-8JK2J2`).
   - Si hace falta, `POST orders/sendVoucher`.

Notas críticas del flujo con link:

- No entregar el voucher al cliente antes de confirmar pago (`USED`).
- Con `waitForPayment: "Y"` **no** se envía el voucher por mail al crear, aunque `sendMail` sea `Y`.
- Tras el pago, el mail automático al titular solo ocurre si el plan tiene activo el envío por correo; si no, usar `sendVoucher`.
- El cliente puede corregir datos en la página de pago; después del pago, tomar los datos definitivos de **Consultar emisión**.
- Un link cubre **una sola** emisión. Con `individualVoucher: true` y varios beneficiarios, cada código tiene su propio link.
- Cada llamada a `paymentLink` **invalida** el link anterior (sirve para regenerar vencidos).

---

## Emisión de vouchers

### Catálogo previo: tipos de documento

`GET {base}/orders/type_documents`

Headers: `Token` (requerido), `Language` (opcional).

Ejemplo de ítems: `PEP`, `PPT`, `PTP`, `NIT`, `PAS`, etc. (`documentType` + `description`).

### POST Realizar emisión

`POST {base}/orders/create`

Crea la cobertura/voucher. Para usar la plataforma de pagos, mandar `waitForPayment: "Y"`.

Ejemplo:

```json
{
  "idPlan": "1369",
  "origin": "CO",
  "destination": "9",
  "startDate": "2024/09/10",
  "endDate": "2024/09/24",
  "daysRange": "30",
  "nameContact": "Paola Perez",
  "emailContact": "perez@mail.com",
  "codePhoneContact": "58",
  "phoneContact": "444-4444",
  "reference": "",
  "individualVoucher": true,
  "comment": "",
  "waitForPayment": "Y",
  "sendMail": "Y",
  "beneficiaries": [
    {
      "name": "Maria",
      "lastName": "Perez",
      "birthdate": "1997/09/11",
      "country": "CO",
      "typeDocument": "PAS",
      "document": "546454654",
      "email": "perez@mail.com",
      "codePhone": "57",
      "phone": "3160000000",
      "sex": "f",
      "medicalConditions": "Problemas de Asma leve"
    }
  ],
  "upgrades": [
    { "id": "326", "beneficiarie": [1] },
    { "id": "417", "beneficiarie": null }
  ]
}
```

#### Campos principales

| Campo | Requerido | Notas |
| --- | --- | --- |
| `idPlan` | Sí | Plan a emitir |
| `origin` | Sí | ISO país origen |
| `destination` | Sí | Id territorio destino |
| `startDate` / `endDate` | Sí | Vigencia; `endDate` >= `startDate`; no anterior a hoy |
| `nameContact` / `emailContact` / `phoneContact` | Sí | Contacto de la orden |
| `codePhoneContact` | No* | Prefijo del contacto |
| `beneficiaries` | Sí | Al menos uno |
| `daysRange` | Condicional | Planes anuales: 30, 45, 60, 90, 120 según config |
| `waitForPayment` | No | `"Y"` = orden pendiente de pago |
| `individualVoucher` | No | `true` + varios pax → un voucher por beneficiario (`BASE-0001`, `BASE-0002`…) |
| `sendMail` | No | `Y`/`N`; default `Y` (pero con waitForPayment no manda hasta pagar) |
| `upgrades` | No | `id` + `beneficiarie` (índices 1..N o `null` si es general) |
| `reference` / `comment` | No | |

Cada beneficiario requiere: `name`, `lastName`, `document`, `birthdate`, `country`, `typeDocument`, `email`, `phone`.  
Opcionales: `codePhone`, `sex`, `medicalConditions`, `comercialFactor` (si el plan/sistema lo exige).

#### Respuesta con pago pendiente

```json
{
  "status": "OK",
  "data": {
    "voucherCode": ["XX-KJICLL"],
    "orderStatus": 2,
    "waitForPayment": "Y",
    "voucher": null,
    "total": "74.00"
  }
}
```

- `orderStatus: 2` ≈ pendiente.
- `voucher: null` hasta confirmar pago.
- `total` como string con 2 decimales.

Si se emite **sin** `waitForPayment` (o con `"N"`), la respuesta puede traer `voucher` con URL de impresión y `orderStatus: 1` (activa).

### Emisión grupal (referencia)

`POST {base}/orders/group` — misma idea, mínimo **10** pasajeros, campo extra `name` (nombre del grupo). También acepta `waitForPayment`.

### Anular emisión

`PATCH {base}/orders/cancel`

```json
{ "code": "XX-0B5M7L" }
```

### Consultar emisión (clave para el polling de pago)

`GET {base}/orders/order/{orderCode}`

También existe `POST {base}/orders/order` con filtros (`birthday` requerido en POST; opcionales `id`, `name`, `lastname`).

Campo útil: `paid` → `1` pagada / `0` no pagada.

Nodo de plataforma de pagos en la respuesta:

```json
"paymentLink": {
  "url": "https://plataforma.com/processLink/48213057",
  "status": "ACTIVE",
  "createdAt": "2026-10-26T14:00:00-05:00",
  "expiresAt": "2026-10-27T14:00:00-05:00"
}
```

`paymentLink` puede ser `null` si la emisión no tiene link.

### Enviar voucher por correo

`POST {base}/orders/sendVoucher`

```json
{
  "code": "AB-123",
  "email": "user@gmail.com"
}
```

Respuesta exitosa: `{ "status": "OK" }`.

### Condicionado / beneficiarios (auxiliar)

- `GET orders/conditions/{orderCode}` → `{ "link": "..." }`
- `GET orders/beneficiaries/{orderCode}` → listado de beneficiarios

---

## Plataforma de pagos

Base: `{base}/payments/`

Hay **cuatro** caminos de pago documentados:

| Método | Endpoint | Uso |
| --- | --- | --- |
| Link de pago (plataforma) | `POST payments/paymentLink` | Cliente paga en la web de New Travel |
| Verificar (pre-chequeo) | `POST payments/verify_payment` | Validar montos/cupón antes de cobrar con tarjeta vía API |
| Procesar tarjeta | `POST payments/process` | Cobrar con datos de tarjeta por API |
| Reportar pago externo | `POST payments/report` | Avisar un pago hecho fuera (transferencia, otra pasarela, etc.) |

Para integración tipo “checkout de New Travel”, el camino principal es **paymentLink** + polling.

### 1) Generar link de pago

`POST {base}/payments/paymentLink`

```json
{
  "code": "CCD-KJICLL",
  "email": "cliente@correo.com"
}
```

| Campo | Requerido | Notas |
| --- | --- | --- |
| `code` | Sí | Una sola emisión (no array). Un link = un voucher |
| `email` | No | Si se envía, la plataforma manda el link por mail; si no, solo viene en la respuesta |

Respuesta:

```json
{
  "status": "OK",
  "data": {
    "code": "CCD-KJICLL",
    "orderStatus": 2,
    "total": "74.00",
    "paymentLink": {
      "url": "https://plataforma.com/processLink/48213057",
      "status": "ACTIVE",
      "createdAt": "2026-10-26T14:00:00-05:00",
      "expiresAt": "2026-10-27T14:00:00-05:00"
    }
  }
}
```

#### Estados del link

| Estado | Significado | Acción |
| --- | --- | --- |
| `ACTIVE` | Vigente; el cliente puede pagar | Seguir consultando |
| `USED` | Cliente pagó; emisión activa | Parar polling; entregar voucher |
| `EXPIRED` | Venció, se reemplazó o se bloqueó por intentos fallidos | Regenerar link si el cliente sigue queriendo pagar |

#### Polling recomendado (sin webhooks)

Consultar `paymentLink` en **Consultar emisión**:

- Cada **30 s** durante los primeros **15 min** después de entregar el link.
- Después, cada **5 min** hasta `expiresAt`.
- Parar cuando sea `USED`, o `EXPIRED` si no se regenera.

Errores frecuentes del link:

| Código | Caso |
| --- | --- |
| `2000` | Falta `code` |
| `2025` | `code` enviado como arreglo |
| `2018` | Emisión inexistente o de otras credenciales |
| `2004` | Correo inválido |
| `3025` | Método de pago no disponible/inactivo |
| `3026` | Emisión ya activa |
| `3027`–`3032` | No admite pago (expirada, anulada, ya pagada, reportada, no emitida por API, etc.) |
| `3052` | Agencia sin pago con tarjeta habilitado (también aplica a verify/process) |

### 2) Verificar pago (antes de process)

`POST {base}/payments/verify_payment`

```json
{
  "code": "CCD-73HI63",
  "couponCode": "C847CMM8J0"
}
```

`code` puede ser string o array de códigos (pago múltiple).  
Con cupón de monto fijo en pago múltiple, el descuento se prorratea por proporción del total.

Respuesta típica (válido):

```json
{
  "status": "OK",
  "data": {
    "paymentValidity": "valid",
    "totalToPaid": "80.00",
    "subtotal": "120.00",
    "couponApplied": true,
    "totalDiscount": "40.00",
    "currency": "USD",
    "orders": [
      {
        "code": "CCD-73HI63",
        "orderStatus": 1,
        "total": "80.00",
        "subTotal": "120.00",
        "discount": "40.00",
        "validity": "valid"
      }
    ]
  }
}
```

### 3) Procesar pago con tarjeta (API)

`POST {base}/payments/process`

```json
{
  "code": "CCD-111111",
  "couponCode": "C847CMM8J0",
  "cardNumber": "0000000000000000",
  "cardName": "Jhon",
  "cardLastname": "Doe",
  "cardType": "Discover",
  "cardCvv": "123",
  "cardExpiry": "05-2027"
}
```

Requeridos: `code`, `cardName`, `cardLastname`, `cardType`, `cardExpiry` (`MM-YYYY`), `cardNumber`, `cardCvv`.  
Opcional: `couponCode`.

Éxito (`paymentStatus: "completed"`): incluye `totalPaid`, órdenes y `payData` (tarjeta enmascarada, etc.).  
Declinado: `paymentStatus: "declined"` + `error` (ej. `4005` Transacción rechazada).  
Nota: una declinación puede venir con `status: "OK"` a nivel raíz y el detalle en `data`/`error`.

Errores de tarjeta (`4XXX`):

| Código | Descripción |
| --- | --- |
| `4000` | Número de tarjeta inválido |
| `4001` | Fecha de caducidad no válida |
| `4002` | Tarjeta caducada |
| `4003` | Transacción duplicada |
| `4004` | Tipo de tarjeta no aceptado |
| `4005` | Transacción rechazada |
| `4006` | Error al procesar la transacción |

### 4) Reportar pago externo

Cuando el cobro se hizo **fuera** de la pasarela integrada (otra pasarela, transferencia, etc.) y hay que marcar la emisión como pagada/activa en New Travel.

`POST {base}/payments/report`

```json
{
  "code": "CCD-ABC123",
  "paymentCode": "EXT-12345",
  "details": "Pago realizado mediante transferencia bancaria el 2024-06-15"
}
```

| Campo | Requerido | Notas |
| --- | --- | --- |
| `code` | Sí | Código de emisión |
| `paymentCode` | Sí | Referencia del pago externo |
| `details` | No | Texto libre |

Respuesta:

```json
{
  "status": "OK",
  "data": {
    "paymentStatus": "completed",
    "code": "CCD-ABC123",
    "paymentCode": "EXT-12345",
    "total": "80.00",
    "currency": "US$"
  }
}
```

Efecto: emisión marcada como pagada y **activada automáticamente**.

---

## Errores relevantes a emisión / pago

### Generales / sesión (`1XXX`)

| Código | Descripción |
| --- | --- |
| `1000` | Usuario o contraseña incorrectos |
| `1001` | Credenciales no válidas |
| `1002` | IP no autorizada / no corresponde al token |
| `1003` | Sesión del token expirada |
| `1004` | Sin permisos |
| `1005` | Registro no encontrado |

### Validación (`2XXX`) — extracto

| Código | Descripción |
| --- | --- |
| `2000` | Faltan campos requeridos |
| `2001` | Fecha o formato no válido (`Y/m/d`) |
| `2003` | Tipo de documento no válido |
| `2004` | Email no válido |
| `2005` | Teléfono no válido |
| `2008` | Plan no válido |
| `2018` | Código de orden no válido |
| `2026` | Ya existe emisión con mismos datos/beneficiarios |

### Emisión / cobro (`3XXX`) — extracto pago

| Código | Descripción |
| --- | --- |
| `3025` | Método de pago no disponible o inactivo |
| `3026` | La emisión ya se encuentra activa |
| `3027` | La emisión ha expirado |
| `3028` | La emisión se encuentra anulada |
| `3029` | Emisión de prueba |
| `3030` | Esta emisión ya ha sido pagada |
| `3031` | No se puede pagar una emisión reportada |
| `3032` | Sólo es posible pagar órdenes emitidas por webservice |
| `3052` | Agencia sin forma de pago con tarjeta habilitada |

Lista completa: `GET {base}/generals/errors`.

---

## Checklist de integración (solo pagos + voucher)

1. Auth + renovar token cada < 6 min (misma IP del servidor).
2. Emitir con `orders/create` + `waitForPayment: "Y"`.
3. Guardar `voucherCode[]` y `total`.
4. Generar `payments/paymentLink` y exponer `url` al cliente.
5. Polling de `GET orders/order/{code}` → `paymentLink.status`.
6. En `USED`: consultar emisión, armar `voucher_url` = `{host}/print/{code}`,
   guardar datos, opcional `sendVoucher` (solo manda mail; no devuelve URL).
7. En `EXPIRED`: regenerar link o abortar.
8. Alternativas:
   - Cobro con tarjeta propio vía API: `verify_payment` → `process`.
   - Cobro en nuestra pasarela: emitir con waitForPayment y luego `payments/report`.

---

## Qué queda fuera de este documento

Cotización (`orders/quote`), listado de planes/beneficios/primas, territorios, monedas, precompras, extensión de vigencia, emisión reportada (`orders/report`), SmartDelay/flights y reportes de ventas. Se pueden documentar aparte cuando se integre la cotización New Travel al multicotizador.
