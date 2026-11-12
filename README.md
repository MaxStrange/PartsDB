# PartsDB

My parts database for electrical components. It compares KiCad BOMs against what's in stock, reserves parts per project, and tracks them in and out with a barcode scanner.

No distributor accounts or API keys are needed: everything it scans is read from the 2D barcode on the bag. Data is stored in one SQLite file. See [docs/requirements.md](docs/requirements.md) for the workflow and the rules behind it.

## Setup

```bash
pip install -e ".[dev]"
```

## Run

```bash
python -m partsdb
```

The database is `~/Dropbox/3DPrints/parts.db`, so it's backed up and survives losing the computer. The status bar shows the path. If that file is missing at startup, the app asks before creating an empty one, because on a new computer Dropbox may still be syncing it. Use `--db path/to/parts.db` to choose another file.

Only run the app on one computer at a time. SQLite isn't built for a file that Dropbox syncs while two copies are open, and Dropbox would make a conflicted copy.

## Test

There's no CI, so run these before pushing:

```bash
python -m pytest
```

## Preparing KiCad

Add an `MPN` field to your symbols and include it in the BOM export. Scans are matched to BOM lines by manufacturer part number. `DigiKey PN` and `Mouser PN` columns are carried through to the order export if present. Export to CSV from the KiCad Symbol Fields Table or BOM tool, or with:

```bash
kicad-cli sch export bom --fields 'Reference,Value,Footprint,${QUANTITY},${DNP},MPN,Manufacturer' --labels 'Reference,Value,Footprint,Qty,DNP,MPN,Manufacturer' --group-by 'Value,Footprint,MPN' -o board-bom.csv board.kicad_sch
```

## Scanner setup

You need a 2D imager that works as a USB keyboard. Scan a bag in the **Scan** tab and read the "Last scan as received" line. It should show `␝` symbols between fields. If they're missing, the scanner isn't sending control characters and the app will say so rather than guess; check the keyboard settings in the scanner's manual.

## License

MIT. The barcode parser is written from the public label format and contains no code from GPL projects.
