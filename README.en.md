# Envia.com shipping connector for Odoo 19 Community

Odoo shipping method (`delivery.carrier`, provider **Envia.com**) built on the public Envia.com REST API.
Quotes at checkout, generates labels on delivery validation, exposes a public tracking page and
validates postal codes in the address forms. Works with any carrier Envia.com offers in your country
(Correo Argentino, Andreani, OCA, DHL, FedEx, Estafeta, …).

Developed and used in production by a small manufacturer shipping from Argentina to customers
worldwide. Licensed LGPL-3. [README en español](README.md).

## Features

| Area | What it does |
|---|---|
| Rates | `POST /ship/rate/` per order at checkout. Price converted to the order currency. Delivery estimate shown as a hint. Refuses to quote (instead of showing a wrong price) when the carrier cannot take a multi-package shipment or when DDP customs data is incomplete. |
| Labels | `POST /ship/generate/` when the delivery order is validated. Label PDF attached to the picking, tracking number stored. Validation runs inside a savepoint: if the label fails, nothing is validated and a wizard offers *retry / validate without label / cancel*. |
| Pickup points ("a sucursal") | Services whose code ends in `suc` deliver to a branch. The customer picks the branch at checkout using Odoo 19's native pickup-point selector, fed from `GET /branches/{country}?zipcode=…&carrier=…`. Manual override per contact (`Envia Branch Code`). |
| International / customs | Per-product HS code, customs description and declared (insurance) value; `customsSettings` with duties payment (recipient / sender / prepaid DDP) and export reason. DDP quotes are rejected if Envia returns no landed cost, so you never pay the customer's duties by surprise. |
| Package overrides | Per picking: weight, dimensions and declared value overrides, with a live preview of what will be sent. Validation of Envia's limits (0.01–10 kg, 1–100 cm). |
| Address book | Origin and destination addresses synced to the Envia address book (ids cached on partner/carrier). Addresses normalised through the Geocodes API (state codes, Colombian DANE codes). |
| Tracking | `/track-envia/<ref>` public page (live `generaltrack` events) + tracking link on the picking. |
| Postal code validation | Address forms (checkout and portal) validate the postal code against Envia's per-country rules (`generic-form`) and existence (`geocodes`), inline (JS) and on submit (server). Fails open if Envia is unreachable. |
| Error messages | Envia error codes (1125, 1126, 1127, 1129, 1170, 1220, 1300) mapped to actionable, translatable messages; provider name hidden from customers at checkout. |

## Setup

1. Install the module (depends on `stock_delivery`, `website_sale`, `portal`).
2. Inventory → Configuration → Shipping Methods → new method, provider **Envia.com**:
   sandbox/production API keys, carrier code (e.g. `correoArgentino`), service code (e.g. `standard_dom`
   or `standard_suc`), default package type. Use *Fetch Available Carriers* to list codes.
3. For international shipments: fill HS code, country of origin, customs description and declared
   value on products; choose the duties payment mode on the method.
4. Optional: `Envia Branch Code` on a contact forces a destination branch.

## Notes

- Odoo 19 only (uses `_validate_address_values`, `pickup_location_data`, `type='jsonrpc'` routes).
  The Odoo 17 version lives in branch `17.0` of this repository.
- The `/branches` endpoint returns at most 300 entries unfiltered; always query with `zipcode`.
- No data about the publisher is embedded: origin address comes from the warehouse, keys from the method.
