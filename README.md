# Conector de envíos Envia.com para Odoo 19 Community

Método de envío de Odoo (`delivery.carrier`, proveedor **Envia.com**) construido sobre la API REST pública de
Envia.com. Cotiza en el checkout, genera etiquetas al validar la entrega, publica una página de seguimiento y
valida los códigos postales en los formularios de dirección. Funciona con cualquier transportista que
Envia.com ofrezca en tu país (Correo Argentino, Andreani, OCA, DHL, FedEx, Estafeta, …).

Desarrollado y usado en producción por un pequeño fabricante que despacha desde Argentina a clientes de todo
el mundo. Licencia LGPL-3. [README in English](README.en.md).

## Funcionalidades

| Área | Qué hace |
|---|---|
| Cotización | `POST /ship/rate/` por pedido en el checkout. Precio convertido a la moneda del pedido. Estimación de entrega como aviso. Se niega a cotizar (en vez de mostrar un precio equivocado) cuando el transportista no acepta envíos multibulto o cuando faltan datos aduaneros para DDP. |
| Etiquetas | `POST /ship/generate/` al validar la orden de entrega. Etiqueta PDF adjunta al picking, número de seguimiento guardado. La validación corre dentro de un savepoint: si la etiqueta falla no se valida nada y un asistente ofrece *reintentar / validar sin etiqueta / cancelar*. |
| Puntos de retiro (“a sucursal”) | Los servicios cuyo código termina en `suc` entregan en sucursal. El cliente elige la sucursal en el checkout con el selector nativo de puntos de retiro de Odoo 19, alimentado por `GET /branches/{país}?zipcode=…&carrier=…`. Anulación manual por contacto (`Código de sucursal Envia`). |
| Internacional / aduana | Código HS, descripción aduanera y valor declarado (seguro) por producto; `customsSettings` con pagador de impuestos (destinatario / remitente / DDP prepago) y motivo de exportación. Las cotizaciones DDP se rechazan si Envia no devuelve el costo en destino (*landed cost*), para no pagar los impuestos del cliente por sorpresa. |
| Anulaciones por bulto | Por entrega: peso, dimensiones y valor declarado, con vista previa de lo que se va a enviar. Validación de los límites de Envia (0,01–10 kg, 1–100 cm). |
| Libreta de direcciones | Direcciones de origen y destino sincronizadas con la libreta de Envia (ids cacheados en contacto/transportista). Direcciones normalizadas con la API Geocodes (códigos de provincia, códigos DANE de Colombia). |
| Seguimiento | Página pública `/track-envia/<ref>` (eventos en vivo de `generaltrack`) y enlace de seguimiento en el picking. |
| Validación de código postal | Los formularios de dirección (checkout y portal) validan el código postal contra las reglas por país de Envia (`generic-form`) y su existencia (`geocodes`), en línea (JS) y al enviar (servidor). Si Envia no responde, deja pasar. |
| Mensajes de error | Códigos de error de Envia (1125, 1126, 1127, 1129, 1170, 1220, 1300) traducidos a mensajes accionables; el nombre del proveedor no se muestra al cliente en el checkout. |

## Configuración

1. Instalar el módulo (depende de `stock_delivery`, `website_sale`, `portal`).
2. Inventario → Configuración → Métodos de envío → nuevo método, proveedor **Envia.com**: claves API de
   sandbox/producción, código de transportista (p. ej. `correoArgentino`), código de servicio (p. ej.
   `standard_dom` o `standard_suc`), tipo de paquete por defecto. *Obtener transportistas disponibles* lista los códigos.
3. Para envíos internacionales: completar código HS, país de origen, descripción aduanera y valor declarado en
   los productos; elegir el modo de pago de impuestos en el método.
4. Opcional: `Código de sucursal Envia` en un contacto fuerza la sucursal de destino.

## Notas

- Solo Odoo 19 (usa `_validate_address_values`, `pickup_location_data` y rutas `type='jsonrpc'`).
  La versión para Odoo 17 está en la rama `17.0` de este repositorio.
- El endpoint `/branches` devuelve como máximo 300 entradas sin filtro; consultar siempre con `zipcode`.
- No incluye ningún dato del editor: la dirección de origen sale del almacén y las claves del método de envío.
