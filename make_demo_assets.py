"""Generate clearly marked fictional PDF/image fixtures for the five-minute demo."""
from pathlib import Path
from io import BytesIO
import fitz
from PIL import Image, ImageDraw

folder = Path(__file__).parent / "samples" / "demo"
folder.mkdir(parents=True, exist_ok=True)
common = "Vendor: Acme Supplies\nInvoice Date: 2026-11-10\nPO Number: PO-1001\n"
examples = {
    "01_clean_invoice.pdf": "TAX INVOICE\nInvoice Number: DEMO-001\n" + common + "Subtotal: 820.00\nTax: 180.00\nGrand Total: 1000.00",
    "02_duplicate_invoice.pdf": "TAX INVOICE\nInvoice Number: DEMO-001\n" + common + "Subtotal: 820.00\nTax: 180.00\nGrand Total: 1000.00",
    "03_po_overage.pdf": "TAX INVOICE\nInvoice Number: DEMO-002\n" + common + "Subtotal: 1000.00\nTax: 180.00\nGrand Total: 1180.00",
    "04_missing_po.pdf": "TAX INVOICE\nInvoice Number: DEMO-003\nVendor: Acme Supplies\nInvoice Date: 2026-11-10\nSubtotal: 820.00\nTax: 180.00\nGrand Total: 1000.00",
    "06_delivery_challan.pdf": "DELIVERY CHALLAN\nChallan No: DEL-004\n" + common + "Goods delivered: Office equipment",
}
for name, content in examples.items():
    doc=fitz.open(); page=doc.new_page()
    page.insert_text((60,65), "FICTIONAL DEMO DOCUMENT\n\n"+content, fontsize=12)
    doc.save(folder / name); doc.close()

image=Image.new("RGB",(1000,650),"white")
draw=ImageDraw.Draw(image)
for i,line in enumerate(("FICTIONAL DEMO DOCUMENT", "TAX INVOICE", "Invoice Number: DEMO-SCAN-1",
                        "Vendor: Acme Supplies", "Invoice Date: 2026-11-10", "PO Number: PO-1004",
                        "Subtotal: 820.00", "Tax: 180.00", "Grand Total: 1000.00")):
    draw.text((45,45+i*55),line,fill="black")
image.save(folder / "05_scanned_invoice.png")
print(f"Created {len(examples)+1} fictional demo documents in {folder}")
