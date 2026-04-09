"""Adaptador para Universal Assistance (SOAP - LeadCotizadorOper).

Precios en DatosLeadCotizadorOut: PrecioEmision (lista, ej. USD), PrecioEmisionLocal (moneda local),
TipoCambio, MonedaLista, MonedaLocal.
"""

import logging
import re
from decimal import Decimal, InvalidOperation
from xml.etree import ElementTree as ET

import httpx

from app.core.config import (
    get_settings,
    get_universal_destinos,
    get_universal_trip_types,
)
from app.quotations.schemas import Benefit, QuotePlan, QuoteRequest

logger = logging.getLogger(__name__)

_SOAP_NS = "http://schemas.xmlsoap.org/soap/envelope/"
_SOAP_ACTION = "document/http://siebel.com/CustomUI:LeadCotizadorOper"


class UniversalQuoteProvider:
    company_name = "Universal"

    def __init__(self) -> None:
        self._settings = get_settings()
        self._destinos = get_universal_destinos(self._settings)
        self._trip_types = get_universal_trip_types(self._settings)

    def get_quotes(self, request: QuoteRequest) -> list[QuotePlan]:
        settings = self._settings
        if not settings.universal_username or not settings.universal_password:
            logger.debug("Universal: faltan UNIVERSAL_USERNAME/UNIVERSAL_PASSWORD")
            return []
        if not settings.universal_organizacion_emisora:
            logger.debug("Universal: falta UNIVERSAL_ORGANIZACION_EMISORA")
            return []

        if request.origin != "AR":
            logger.debug("Universal: origen '%s' no soportado (solo AR configurado)", request.origin)
            return []
        if not settings.universal_pais_origen_ar:
            logger.debug("Universal: falta UNIVERSAL_PAIS_ORIGEN_AR")
            return []

        destino = self._destinos.get(request.destination_id)
        if not destino:
            logger.debug(
                "Universal: destination_id=%s sin mapeo UNIVERSAL_DESTINO_*",
                request.destination_id,
            )
            return []

        tipo_viaje = self._trip_types.get(request.trip_type)
        if not tipo_viaje:
            logger.debug(
                "Universal: trip_type='%s' sin mapeo UNIVERSAL_TIPO_VIAJE_*",
                request.trip_type,
            )
            return []

        if len(request.ages) > 10:
            logger.debug("Universal: más de 10 pasajeros no soportado por el WS")
            return []

        soap_envelope = self._build_envelope(request, destino, tipo_viaje)
        headers = {
            "Content-Type": "text/xml;charset=UTF-8",
            "Accept": "text/xml",
            "SOAPAction": f'"{_SOAP_ACTION}"',
        }

        with httpx.Client(timeout=30.0) as client:
            response = client.post(settings.universal_soap_url, content=soap_envelope, headers=headers)
            if response.status_code >= 400:
                logger.error(
                    "Universal: HTTP %s - body=%s",
                    response.status_code,
                    response.text,
                )
            response.raise_for_status()
            return self._parse_response(response.text)

    def _build_envelope(self, request: QuoteRequest, destino: str, tipo_viaje: str) -> str:
        settings = self._settings
        edades = request.ages[:10] + [None] * (10 - len(request.ages))
        cant_cotizaciones = "0"
        convenio = settings.universal_convenio
        folleto = settings.universal_folleto

        def age_tag(i: int, age: int | None) -> str:
            val = "" if age is None else str(age)
            return f"<ual:Edad{i}>{val}</ual:Edad{i}>"

        edades_xml = "".join(age_tag(i + 1, edades[i]) for i in range(10))
        return (
            '<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" '
            'xmlns:cus="http://siebel.com/CustomUI" '
            'xmlns:ual="http://www.siebel.com/xml/UALeadCotizadorReq">'
            "<soapenv:Header>"
            '<wsse:Security xmlns:wsse="http://schemas.xmlsoap.org/ws/2002/07/secext">'
            '<wsse:UsernameToken xmlns:wsu="http://schemas.xmlsoap.org/ws/2002/07/utility">'
            f"<wsse:Username>{settings.universal_username}</wsse:Username>"
            f'<wsse:Password Type="wsse:PasswordText">{settings.universal_password}</wsse:Password>'
            "</wsse:UsernameToken>"
            "</wsse:Security>"
            "</soapenv:Header>"
            "<soapenv:Body>"
            "<cus:LeadCotizadorOper_Input>"
            "<ual:UALeadCotizadorReq>"
            "<ual:DatosLeadCotizadorIn>"
            "<ual:IdLead></ual:IdLead>"
            f"<ual:OrganizacionEmisora>{settings.universal_organizacion_emisora}</ual:OrganizacionEmisora>"
            f"<ual:CantCotizaciones>{cant_cotizaciones}</ual:CantCotizaciones>"
            f"<ual:Convenio>{convenio}</ual:Convenio>"
            f"<ual:Folleto>{folleto}</ual:Folleto>"
            f"<ual:PaisOrigen>{settings.universal_pais_origen_ar}</ual:PaisOrigen>"
            f"<ual:Destino>{destino}</ual:Destino>"
            f"<ual:TipoViaje>{tipo_viaje}</ual:TipoViaje>"
            f"<ual:FechaInicio>{request.departure_date.strftime('%m/%d/%Y')}</ual:FechaInicio>"
            f"<ual:FechaFin>{request.return_date.strftime('%m/%d/%Y')}</ual:FechaFin>"
            f"<ual:CantidadPasajeros>{len(request.ages)}</ual:CantidadPasajeros>"
            "<ual:PackFamiliar></ual:PackFamiliar>"
            f"{edades_xml}"
            "<ual:ApellidoContacto></ual:ApellidoContacto>"
            "<ual:NombreContacto></ual:NombreContacto>"
            "<ual:TelefonoContacto></ual:TelefonoContacto>"
            "<ual:EmailContacto></ual:EmailContacto>"
            "</ual:DatosLeadCotizadorIn>"
            "</ual:UALeadCotizadorReq>"
            "</cus:LeadCotizadorOper_Input>"
            "</soapenv:Body>"
            "</soapenv:Envelope>"
        )

    def _parse_response(self, raw_xml: str) -> list[QuotePlan]:
        try:
            root = ET.fromstring(raw_xml)
        except ET.ParseError:
            logger.error("Universal: XML inválido en respuesta")
            return []

        body = root.find(f"{{{_SOAP_NS}}}Body")
        if body is None:
            logger.warning("Universal: respuesta sin SOAP Body")
            return []

        datos_nodes = [n for n in body.iter() if self._local_name(n.tag) == "DatosLeadCotizadorOut"]
        plans: list[QuotePlan] = []
        for datos in datos_nodes:
            plan = self._datos_to_plan(datos)
            if plan:
                plans.append(plan)
        return plans

    def _datos_to_plan(self, datos: ET.Element) -> QuotePlan | None:
        lead_id = self._child_text(datos, "IdLeadOut").strip()
        if not lead_id:
            return None

        product_id = self._child_text(datos, "IdProducto").strip()
        if not product_id:
            return None

        plan_name = self._child_text(datos, "NombreProducto").strip()
        if not plan_name:
            plan_name = self._child_text(datos, "Producto").strip() or product_id

        precio_usd = self._parse_decimal(self._child_text(datos, "PrecioEmision"))
        if precio_usd is None:
            return None

        precio_local = self._parse_decimal(self._child_text(datos, "PrecioEmisionLocal"))
        tipo_cambio = self._parse_decimal(self._child_text(datos, "TipoCambio"))

        if precio_local is not None:
            final_rate = precio_local.quantize(Decimal("0.01"))
            if tipo_cambio is not None:
                exchange_rate = tipo_cambio.quantize(Decimal("0.0001"))
            elif precio_usd > 0:
                exchange_rate = (precio_local / precio_usd).quantize(Decimal("0.0001"))
            else:
                exchange_rate = Decimal("1")
            net_rate = final_rate
        else:
            final_rate = precio_usd.quantize(Decimal("0.01"))
            exchange_rate = Decimal("1")
            net_rate = precio_usd.quantize(Decimal("0.01"))

        atributos = [n for n in list(datos) if self._local_name(n.tag) == "Atributo"]
        benefits = self._build_benefits(atributos)
        coverage_amount = self._coverage_from_first_attribute(atributos)

        return QuotePlan(
            company=self.company_name,
            # Regla funcional acordada: para Universal usamos solo IdLeadOut como id
            # de cotización (sin concatenar ni usar IdProducto).
            id=lead_id,
            plan_id=product_id,
            plan_name=plan_name,
            coverage_amount=coverage_amount,
            benefits=benefits,
            net_rate=net_rate,
            final_rate_usd=precio_usd.quantize(Decimal("0.01")),
            exchange_rate=exchange_rate,
            final_rate=final_rate,
        )

    def _build_benefits(self, atributos: list[ET.Element]) -> list[Benefit]:
        benefits: list[Benefit] = []
        for idx, attr in enumerate(atributos):
            nombre = self._child_text(attr, "NombreVisible").strip() or self._child_text(attr, "Nombre").strip()
            valor = self._child_text(attr, "Valor").strip()
            unidad = self._child_text(attr, "Unidad").strip()
            if unidad and valor:
                valor = f"{unidad} {valor}"
            benefits.append(Benefit(id=idx, nombre=nombre, valor=valor))
        return benefits

    def _coverage_from_first_attribute(self, atributos: list[ET.Element]) -> Decimal:
        if not atributos:
            return Decimal("0")
        first_valor = self._child_text(atributos[0], "Valor")
        parsed = self._parse_decimal(first_valor)
        return parsed if parsed is not None else Decimal("0")

    def _child_text(self, node: ET.Element, local_name: str) -> str:
        for child in list(node):
            if self._local_name(child.tag) == local_name:
                return (child.text or "").strip()
        return ""

    def _local_name(self, tag: str) -> str:
        if "}" in tag:
            return tag.rsplit("}", 1)[1]
        return tag

    def _parse_decimal(self, raw_value: str | None) -> Decimal | None:
        if raw_value is None:
            return None
        s = raw_value.strip()
        if not s:
            return None
        m = re.search(r"(\d[\d\.,]*)", s)
        if not m:
            return None
        num = m.group(1).replace(" ", "")
        if "," in num and "." in num:
            if num.rfind(",") > num.rfind("."):
                num = num.replace(".", "").replace(",", ".")
            else:
                num = num.replace(",", "")
        elif "," in num:
            num = num.replace(",", ".")
        try:
            return Decimal(num).quantize(Decimal("0.01"))
        except InvalidOperation:
            return None
