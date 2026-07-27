from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


# KYC / onboarding documents collected from a vendor. One current file per
# (vendor, doc_type) — re-uploading the same type replaces the previous file.
# Files live in a private R2 prefix and are served only via short-lived
# presigned URLs (KYC docs contain PII like PAN / bank / Aadhaar details).
#
# Each entry is (doc_type, required). GST and FSSAI are optional because many
# small vendors are below the GST threshold or aren't food businesses — a
# vendor is "complete" once all *required* docs are present.
DOC_SPECS = (
    ("pan_card", True),
    ("bank_proof", True),
    ("aadhaar", True),
    ("gst_certificate", False),
    ("fssai_license", False),
    # Optional slots for registered-entity (company/LLP) vendors. A sole
    # proprietor leaves these empty; KYC completeness only counts required docs.
    ("company_pan", False),
    ("incorporation_cert", False),  # CoI / MGT-14 / CIN proof
    ("cancelled_cheque", False),
)
DOC_TYPES = tuple(doc_type for doc_type, _ in DOC_SPECS)
REQUIRED_DOC_TYPES = frozenset(doc_type for doc_type, required in DOC_SPECS if required)


class VendorDocument(Base):
    __tablename__ = "vendor_documents"
    __table_args__ = (UniqueConstraint("vendor_id", "doc_type", name="uq_vendor_doc_type"),)

    id: Mapped[str] = mapped_column(String(30), primary_key=True)
    vendor_id: Mapped[str] = mapped_column(
        String(30), ForeignKey("vendors.id", ondelete="CASCADE"), index=True
    )
    doc_type: Mapped[str] = mapped_column(String(40))  # one of DOC_TYPES
    # R2 object key (not a public URL) — view links are presigned on demand.
    file_key: Mapped[str] = mapped_column(String(500))
    file_name: Mapped[str | None] = mapped_column(String(255))
    uploaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    vendor: Mapped["Vendor"] = relationship(back_populates="documents")
