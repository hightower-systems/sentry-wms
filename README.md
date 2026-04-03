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

## Project Status

🚧 **Phase 1 - Foundation** (in progress)

See [CHANGELOG.md](CHANGELOG.md) for current progress.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines.

## License

MIT - see [LICENSE](LICENSE) for details.

Built by [Hightower Systems L.L.C.](https://github.com/hightower-systems)
