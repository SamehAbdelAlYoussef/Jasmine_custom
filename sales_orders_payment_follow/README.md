# Smart Buttons (Odoo 18)

Updated and optimized version of Smart Buttons module for Odoo 18.

## Summary
This version adds:
1. **Immediate recomputation** of related sale orders and partners when account payments are created, modified, or deleted.
2. **Detailed logging** for all actions and computed fields (search, write, unlink).
3. **Performance optimization** using `read_group` in partner computations.
4. **Odoo 18 compatibility** — validated structure, manifest update, and code modernization.

## Installation
1. Copy this folder to your Odoo 18 addons directory.
2. Update the apps list from Odoo backend.
3. Install **Smart Buttons**.

## Logging
All logs are prefixed with `[Smart Buttons]` and visible in your Odoo server logs.

---
_Last updated: 2025-10-19 10:50:26_


## Tests
A basic automated test is included in `/tests/test_smart_buttons.py`.
Run it using Odoo's test framework:

```
odoo -i smart_buttons --test-enable --stop-after-init
```
