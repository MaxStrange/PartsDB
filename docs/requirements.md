# Requirements

## Workflow

1. Generate the BOM in KiCad.
2. Import it. Each line is matched to stock by manufacturer part number (MPN).
3. For each line the app shows how many are free in stock and suggests using them. Adjust **Use from stock** line by line; **To order** is whatever remains.
4. Export the order as CSV and upload it to DigiKey's BOM tool.
5. Buy the parts by hand from DigiKey, then Mouser or anyone else for what's left.
6. **Mark as ordered and reserve parts.** The stock portion of each line becomes *reserved* and the rest becomes *pending*. Parts not yet in the database are created. The BOM is locked from here.
7. As deliveries arrive, scan bags in. Pending parts become reserved.
8. While building, scan parts out and enter how many were used, including any lost. On-hand stock goes down.
9. Complete the project.

## Stock rules

- **On hand** is the physical count. **Free** is on hand minus everything reserved across all projects. Free stock is what planning offers, so two projects can't claim the same parts.
- A line's quantity is tracked per state (pending, reserved, used), so "need 10, have 3" is 3 reserved and 7 pending.
- Reservations are made at step 6, not at import. Projects still in planning don't hold stock, so the commit re-checks free stock and refuses if it has since been claimed.

## Scanning in

- A bag fills pending lines for that part, oldest project first. Anything left over becomes free stock (need 7, bag of 100: 7 reserved, 93 free).
- A part that no project expects prompts for either free stock or **substitute for** an open line. The line keeps its original MPN and shows the substitute. A partial substitute leaves the remainder pending on the original part. If nothing is open, it goes to free stock with no prompt, which covers general stock intake.
- Re-scanning a label seen before warns and asks. It isn't rejected, because identical bags can carry identical labels.
- Parts without a compatible barcode (SparkFun, Adafruit, etc.) are typed or scanned as plain text, and the app asks for a quantity.

## Scanning out

- The label's quantity is never used, because it's what the bag held when new. The prompt defaults to the amount still reserved for the project.
- Reservations are used first. Going over draws on free stock, and a warning appears if that cuts into another project's reservation. Using more than is on hand is refused; correct the count in Inventory first.
- A part that isn't on the selected BOM asks for confirmation. "(no project)" covers general use.

## Closing a project

Completing or cancelling returns unused reservations to free stock and drops anything still pending. Used quantities stay on record. Lines marked DNP never allocate anything, and any line without an MPN must get one or be marked DNP before reserving.

## Corrections

- **Undo last** reverses the most recent stock move, including its effect on reservations. It's refused if the project it touched has since been closed.
- **Adjust stock** in Inventory sets a part's count directly. Every stock change is listed in History.

## Not in the first version

- Distributor APIs (order import, automatic descriptions and datasheets). Nothing above needs them.
- Printing labels for parts without a barcode. Any barcode containing the MPN as plain text will already scan.
- CI. Run `python -m pytest` before pushing.
