"""Modelos de datos normalizados (dataset final)."""
from datetime import datetime, timezone

from pydantic import BaseModel, Field


class Category(BaseModel):
  category_id: int | None = None
  slug: str
  name: str
  url: str


class ProductRecord(BaseModel):
  """Muestra normalizada: primer producto de una categoria de G2."""
  # Contexto de la categoria
  category_id: int | None = None
  category_slug: str
  category_url: str
  category_name: str
  # Producto (fuente primaria: data-event-options del DOM)
  product_id: int | None = None
  product_uuid: str | None = None
  product_name: str
  product_slug: str | None = None
  product_url: str | None = None
  product_type: str | None = None
  vendor_name: str | None = None
  vendor_url: str | None = None
  rating: float | None = None
  reviews_count: int | None = None
  description: str | None = None
  image_url: str | None = None
  # Metadatos de la extraccion
  scraped_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
  attempt: int = 1
  latency_ms: float = 0.0
  # Resolucion via sub-categorias (cuando la categoria padre no lista productos)
  resolved_via_subcategory: bool = False
  subcategory_hops: list[str] = Field(default_factory=list)
  status: str = "success"