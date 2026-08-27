# INTERASSIST API — Integración externa

BASE URL:
https://api-staging.interassist.com

DOCUMENTACIÓN:
https://api-staging.interassist.com/api/docs-externo

AUTENTICACIÓN:
Server-to-server mediante API Key.

Header:
Authorization: Bearer ${INTERASSIST_API_KEY}

La API Key tiene formato:
sk_live_...

NO hardcodear la API Key en el código.
Usar variable de entorno:
INTERASSIST_API_KEY

No se requiere usuario/password para las llamadas server-to-server.


========================================
PLANES
========================================

GET /api/planes/empresas/{paisId}
Descripción:
Obtener empresas por país.

----------------------------------------

GET /api/planes/all/dualbox
Descripción:
Listar planes activos para Dualbox.

----------------------------------------

GET /api/planes/{id}/detalles
Descripción:
Obtener los items y valores de cobertura de un plan.

Ejemplo:
GET /api/planes/57/detalles

IMPORTANTE:
Este endpoint NO cotiza.
Devuelve las coberturas del plan.

----------------------------------------

GET /api/planes
Descripción:
Listar planes paginados.

----------------------------------------

GET /api/planes/{plan}
Descripción:
Obtener un plan por ID.

----------------------------------------

POST /api/planes/empresas
Descripción:
Obtener empresas asociadas a un listado de países.


========================================
VENTAS
========================================

POST /api/ventas/agregar-vouchers
Descripción:
Agregar vouchers a una venta.

----------------------------------------

POST /api/ventas/clientesemision
Descripción:
Obtener clientes según tipo de emisión y empresa.

----------------------------------------

GET /api/ventas/datoscf
Descripción:
Obtener catálogos para datos de consumidor final.

----------------------------------------

GET /api/ventas/datosfacturacion
Descripción:
Obtener catálogo de destino y forma de facturación.

----------------------------------------

GET /api/ventas/datostc
Descripción:
Obtener catálogo de tarjetas de crédito.

----------------------------------------

GET /api/ventas/empresasemision
Descripción:
Obtener datos de las empresas vinculadas al usuario.

----------------------------------------

POST /api/ventas/planesemision
Descripción:
Obtener planes vinculados a una empresa y a un tipo de emisión.

IMPORTANTE:
Este endpoint NO parece ser una cotización.
La respuesta devuelve planes disponibles, por ejemplo:

{
  "data": [
    {
      "id": 57,
      "nombre": "INTER 100",
      "incentivo": 1,
      "edad_maxima": null
    }
  ]
}

No devuelve precio, cantidad de días ni importe de cotización.

----------------------------------------

POST /api/ventas/promocionesemision
Descripción:
Obtener promociones disponibles para una emisión.

PENDIENTE:
Investigar request/response para determinar si participa en el cálculo del precio.

----------------------------------------

GET /api/ventas/empresas
Descripción:
Obtener empresas vinculadas al usuario.

----------------------------------------

GET /api/ventas
Descripción:
Listado paginado de ventas.

----------------------------------------

POST /api/ventas
Descripción:
Crear una venta/emisión y generar los vouchers asociados.

IMPORTANTE:
NO es un endpoint de cotización.
Registra la venta y genera los vouchers.

Respuesta exitosa:

{
  "message": "Venta Creada con Éxito",
  "venta": {
    "id": 88500,
    "voucher_ids": [
      90001
    ]
  }
}

Validaciones:
- empresa_id debe coincidir con el company_id de la API Key.
- El plan debe pertenecer a la empresa.
- Fechas en formato d/m/Y.
- Los campos requeridos dependen del tipo de venta y facturación.

----------------------------------------

GET /api/ventas/{id}
Descripción:
Obtener detalle de una venta.

----------------------------------------

PUT /api/ventas/{id}
Descripción:
Recalcular / actualizar una venta.

PENDIENTE:
Investigar request/response.
Este endpoint puede ser relevante para determinar cómo funciona el cálculo/recalculo de precios.


========================================
VOUCHERS
========================================

POST /api/vouchers/createopenvoucher
Descripción:
Abrir o cerrar un voucher.

----------------------------------------

GET /api/vouchers/{id}/pdf/{lang}
Descripción:
Descargar PDF de un voucher.

Ejemplo:
GET /api/vouchers/90001/pdf/es

----------------------------------------

POST /api/vouchers/saveRescheduled
Descripción:
Reprogramar las fechas de un voucher.

----------------------------------------

GET /vouchers/showaccordinglanguage/{id}/{language}
Descripción:
Mostrar voucher en HTML según idioma.

NOTA:
Esta ruta no tiene /api según la documentación.

----------------------------------------

GET /api/vouchers
Descripción:
Obtener listado paginado de vouchers.

----------------------------------------

GET /api/vouchers/{id}
Descripción:
Obtener voucher por ID.


========================================
ESTADO ACTUAL DE LA INVESTIGACIÓN
========================================

Se necesita implementar una funcionalidad de COTIZACIÓN
de asistencia al viajero.

IMPORTANTE:
La documentación disponible NO muestra un endpoint
explícitamente llamado "cotizar", "quote" o similar.

Endpoints descartados como cotización:

GET /api/planes
-> catálogo de planes.

GET /api/planes/{id}
-> información de un plan.

GET /api/planes/{id}/detalles
-> coberturas del plan, NO precio del viaje.

POST /api/ventas/planesemision
-> lista de planes disponibles para emisión, NO devuelve
   un precio de cotización.

POST /api/ventas
-> EMITE la venta y genera vouchers. No utilizarlo
   simplemente para mostrar una cotización.

Endpoints que todavía deben investigarse:

POST /api/ventas/promocionesemision
PUT /api/ventas/{id}

El backend de Interassist aparentemente tiene lógica
relacionada con "puedeCotizarPlan", por lo que podría
existir lógica de cotización no expuesta claramente
como endpoint en la documentación.


========================================
CONFIGURACIÓN PARA NODE.JS
========================================

Variables de entorno:

INTERASSIST_BASE_URL=https://api-staging.interassist.com
INTERASSIST_API_KEY=sk_live_REEMPLAZAR_CON_API_KEY_REAL

Ejemplo conceptual de headers:

Authorization: Bearer ${INTERASSIST_API_KEY}
Accept: application/json
Content-Type: application/json


========================================
REGLAS PARA CURSOR
========================================

1. NO hardcodear la API Key.
2. NO almacenar la API Key en Git.
3. Usar process.env.INTERASSIST_API_KEY.
4. Usar process.env.INTERASSIST_BASE_URL.
5. Todas las llamadas server-to-server deben enviar:
   Authorization: Bearer <API_KEY>
6. No implementar todavía la cotización hasta determinar
   cuál es el endpoint/mecanismo real de cálculo de precio.
7. No llamar POST /api/ventas para simular una cotización,
   porque ese endpoint genera una venta y vouchers.



   curl -X 'GET' \
  'https://api-staging.interassist.com/api/planes/empresas/14' \
  -H 'accept: application/json' \
  -H 'Authorization: Bearer 398|OUIXIQHJoTdxVAsr0DxxE2S2X2asMLKxjqZmSMRJe1105c38'