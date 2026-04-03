# Sentry WMS

**Open-source warehouse management system built for e-commerce.**

Sentry is the link between the warehouse floor and your system of record. It connects barcode scans, pick tasks, and inventory movements to whatever database or ERP your business runs on.

## What Sentry Does

- **Receiving** - Scan PO barcodes, verify items, stage for put-away
- **Put-Away** - Suggested bin placement, scan-to-confirm storage
- **Picking** - Multi-order batch picking with optimized walk paths
- **Packing** - Scan-to-verify pack workflows
- **Shipping** - Carrier integration, label printing, tracking
- **Cycle Counting** - Bin-level counts with variance detection
- **Bin Transfers** - Move inventory between locations

## What Sentry Is Not

Sentry is not an ERP. It does not manage orders, products, or customers. It connects to your existing systems (NetSuite, QuickBooks, SAP, or any ERP with an API) and handles the physical warehouse execution layer.

## Architecture

| Layer | Technology |
|-------|-----------|
| Mobile App | React Native (Expo) |
| API | Python / Flask |
| Database | PostgreSQL (dev) · Fabric SQL / PostgreSQL Cloud (prod) |
| Admin Panel | React Web App |

## Quick Start

```bash
# Clone the repo
git clone https://github.com/hightower-systems/sentry-wms.git
cd sentry-wms

# Copy environment config
cp .env.example .env

# Start PostgreSQL + API with Docker
docker-compose up -d

# API is now running at http://localhost:5000
# Health check: http://localhost:5000/api/health
```

## API Endpoints

### Authentication
| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/auth/login` | Device login, returns JWT token |
| POST | `/api/auth/refresh` | Refresh an existing token |

### Lookups
| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/lookup/item/<barcode>` | Scan item → details + bin locations |
| GET | `/api/lookup/bin/<barcode>` | Scan bin → contents with quantities |
| GET | `/api/lookup/item/search?q=` | Text search items by SKU, name, UPC |
| GET | `/api/lookup/bin/search?q=` | Text search bins by code |

### Receiving
| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/receiving/po/<barcode>` | Scan PO → lines with expected items |
| POST | `/api/receiving/receive` | Submit received items to staging bin |

### Put-Away
| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/putaway/pending/<warehouse_id>` | Items in staging awaiting put-away |
| GET | `/api/putaway/suggest/<item_id>` | Suggested bin for put-away |
| POST | `/api/putaway/confirm` | Confirm put-away to destination bin |

### Picking
| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/picking/create-batch` | Create pick batch with optimized walk path |
| GET | `/api/picking/batch/<batch_id>` | Full batch with tasks in walk-path order |
| GET | `/api/picking/batch/<batch_id>/next` | Next pending pick task |
| POST | `/api/picking/confirm` | Confirm a pick with barcode validation |
| POST | `/api/picking/short` | Report a short pick |
| POST | `/api/picking/complete-batch` | Mark batch complete |

### Packing
| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/packing/order/<barcode>` | Scan SO → picked items to verify with weight |
| POST | `/api/packing/verify` | Scan item barcode to verify during packing |
| POST | `/api/packing/complete` | Mark order fully packed |

### Shipping
| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/shipping/fulfill` | Submit shipment with tracking + carrier info |

### Cycle Counting
| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/inventory/cycle-count/create` | Create cycle counts for bins with inventory snapshot |
| GET | `/api/inventory/cycle-count/<count_id>` | View count with expected vs counted quantities |
| POST | `/api/inventory/cycle-count/submit` | Submit counts, auto-adjust variances |

### Bin Transfers
| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/transfers/move` | Move items between bins |

## Project Status

🚧 **Active Development** - building toward v1.0.0

| Version | Milestone | Status |
|---------|-----------|--------|
| v0.1.0 | Foundation - project structure, schema, Docker | ✅ Complete |
| v0.2.0 | JWT auth, item/bin lookups | ✅ Complete |
| v0.3.0 | Receiving + put-away | ✅ Complete |
| v0.4.0 | Batch picking with path optimization | ✅ Complete |
| v0.5.0 | Pack + ship | ✅ Complete |
| v0.6.0 | Inventory management (cycle counts, transfers) | ✅ Complete |
| v0.7.0 | Admin panel + polish | 🔨 In progress |
| v0.8.0 | ERP integration + connectors | Planned |
| v1.0.0 | Public release | Planned |

See [CHANGELOG.md](CHANGELOG.md) for detailed release notes.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines.

## License

MIT - see [LICENSE](LICENSE) for details.

Built by [Hightower Systems L.L.C.](https://github.com/hightower-systems)
