# Credenciales por agencia

Cada agencia puede tener su propia identidad en la API de una compañía. Hoy el único formulario es Cardinal. El resto, Omint incluido, sigue con las credenciales genéricas del servidor.

La cotización no cambia de contrato. `POST /v1/quotes` es el mismo. Si la agencia tiene credencial de Cardinal, esa cotización sale con su `agente_emisor_guid`. Si no tiene, sale con el del servidor.

La pantalla es solo para un admin, en la ficha de la agencia. La agencia y el vendedor no llaman estas rutas. El vendedor usa la credencial de su agencia sin verla.

Los campos también están en `/docs`. El body del PUT figura ahí como objeto libre: los nombres de abajo son los que hay que mandar.

## Pantalla

`GET /v1/agencies/{id}/credentials`

```json
{
  "items": [
    {
      "company_slug": "cardinal",
      "company_name": "Cardinal",
      "uses_default": true,
      "credentials": null
    }
  ]
}
```

Hoy `items` tiene una sola compañía. No hay que armar un formulario genérico para las demás.

- `uses_default: true`: no hay credencial propia. El formulario va vacío y se muestra que usa las genéricas. Esos valores no vienen en la respuesta.
- `uses_default: false`: `credentials` trae los valores en claro para que el admin los vea y los edite.

404 si la agencia no existe o está inactiva.

## Guardar

`PUT /v1/agencies/{id}/credentials/cardinal`

Reemplaza todo lo guardado. Hay que mandar los dos campos, aunque el secreto no cambie. Si se manda solo el GUID, el secreto anterior se borra.

```json
{
  "agente_emisor_guid": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
  "agente_emisor_secreto": ""
}
```

`agente_emisor_guid` es obligatorio. `agente_emisor_secreto` puede ir vacío: la cotización de Cardinal no lo envía. Se guarda para la emisión.

La respuesta es el ítem guardado, con `uses_default: false` y `credentials` en claro.

- 422 si falta el GUID.
- 404 si el slug no es `cardinal`.
- 500 si en el servidor falta `PASSWORD_ENCRYPTION_KEY`. No lo setea el panel.

## Volver a las genéricas

`DELETE /v1/agencies/{id}/credentials/cardinal`

Responde 204. El próximo GET vuelve a `uses_default: true` y `credentials: null`.

404 si esa agencia no tenía credencial propia.
