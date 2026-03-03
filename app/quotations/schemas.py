"""
Schemas para cotizaciones.

Mapeo campos respuesta (ES → EN):
    empresa cotizacion  → company
    plan id             → plan_id
    monto de cobertura  → coverage_amount
    prestaciones        → benefits
    tarifa neta         → net_rate
    tarifa final usd    → final_rate_usd
    tasa cambio         → exchange_rate
    tarifa final        → final_rate
"""
from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field, model_validator

# Tipos de viaje: único viaje, multiviaje (anual), larga estadía.
TRIP_TYPE_UNICO_VIAJE = "unico_viaje"
TRIP_TYPE_MULTIVIAJE = "multiviaje"
TRIP_TYPE_LARGA_ESTADIA = "larga_estadia"
TripType = Literal["unico_viaje", "multiviaje", "larga_estadia"]
TRIP_TYPES_VALIDOS: tuple[TripType, ...] = (
    TRIP_TYPE_UNICO_VIAJE,
    TRIP_TYPE_MULTIVIAJE,
    TRIP_TYPE_LARGA_ESTADIA,
)

# IDs de destino del cotizador (el frontend envía estos IDs).
DESTINO_ID_NACIONAL = 1
DESTINO_ID_LATINOAMERICA = 2
DESTINO_ID_EUROPA = 3
DESTINO_ID_RESTO_MUNDO = 4
DESTINO_ID_NORTEAMERICA = 5
DESTINO_IDS_VALIDOS = (1, 2, 3, 4, 5)


class QuoteRequest(BaseModel):
    """Parámetros de entrada para cotizar."""

    departure_date: date = Field(..., description="Fecha ida")
    return_date: date = Field(..., description="Fecha vuelta")
    ages: list[int] = Field(..., min_length=1, description="Edades de los pasajeros")
    origin: str = Field(..., description="Origen (ej. AR para Argentina)")
    destination_id: int = Field(..., description="ID destino: 1=Nacional, 2=Latinoamerica, 3=Europa, 4=Resto del mundo, 5=Norteamerica")
    trip_type: TripType = Field(
        default=TRIP_TYPE_UNICO_VIAJE,
        description="Tipo de viaje: unico_viaje, multiviaje (anual), larga_estadia",
    )

    @model_validator(mode="after")
    def return_after_departure(self):
        if self.return_date < self.departure_date:
            raise ValueError("La fecha de vuelta debe ser posterior a la fecha de ida")
        if self.destination_id not in DESTINO_IDS_VALIDOS:
            raise ValueError(f"destination_id debe ser 1-5, recibido: {self.destination_id}")
        if self.trip_type not in TRIP_TYPES_VALIDOS:
            raise ValueError(f"trip_type debe ser {TRIP_TYPES_VALIDOS}, recibido: {self.trip_type}")
        return self


class Benefit(BaseModel):
    """Prestación del plan (id, nombre, valor)."""

    id: int = Field(..., description="ID de la prestación")
    nombre: str = Field(..., description="Nombre de la prestación")
    valor: str = Field(..., description="Valor/texto de la prestación (ej. USS 50.000)")


class QuotePlan(BaseModel):
    """Un plan/opción de cotización (formato unificado por compañía)."""

    company: str = Field(..., description="Empresa de cotización")
    id: str = Field(..., description="Identificador único de la cotización")
    plan_id: str = Field(..., description="ID del plan")
    plan_name: str = Field(..., description="Nombre del plan (ej. CARDINAL 100)")
    coverage_amount: Decimal = Field(..., description="Monto de cobertura (Tope Máximo Global)")
    benefits: list[Benefit] = Field(default_factory=list, description="Prestaciones del plan")
    net_rate: Decimal = Field(..., description="Tarifa neta (final_rate_usd menos comisión)")
    final_rate_usd: Decimal = Field(..., description="Tarifa final USD")
    exchange_rate: Decimal = Field(..., description="Tasa de cambio")
    final_rate: Decimal = Field(..., description="Tarifa final")


class QuoteResponse(BaseModel):
    """Respuesta del cotizador: lista de planes de todas las compañías."""

    plans: list[QuotePlan] = Field(default_factory=list)
