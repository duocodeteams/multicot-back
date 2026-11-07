"""Catálogo Inter Assist: países (REST Countries) y provincias AR (Georef)."""

from unittest import TestCase
from unittest.mock import MagicMock

import app.services.companies.inter_assist_catalog as catalog


class InterAssistCatalogTest(TestCase):
    def setUp(self) -> None:
        catalog._countries_cache = None
        catalog._ar_provinces_cache = None

    def tearDown(self) -> None:
        catalog._countries_cache = None
        catalog._ar_provinces_cache = None

    def test_parses_countries_and_puts_argentina_first(self) -> None:
        client = MagicMock()
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = [
            {
                "cca2": "UY",
                "name": {"common": "Uruguay"},
                "translations": {"spa": {"common": "Uruguay"}},
            },
            {
                "cca2": "AR",
                "name": {"common": "Argentina"},
                "translations": {"spa": {"common": "Argentina"}},
            },
            {
                "cca2": "BR",
                "name": {"common": "Brazil"},
                "translations": {"spa": {"common": "Brasil"}},
            },
        ]
        response.raise_for_status = MagicMock()
        client.get.return_value = response

        items = catalog.list_inter_assist_countries(client=client)
        self.assertEqual(items[0], {"id": "AR", "name": "Argentina"})
        self.assertEqual(
            [item["id"] for item in items],
            ["AR", "BR", "UY"],
        )

    def test_countries_fallback_on_http_error(self) -> None:
        client = MagicMock()
        client.get.side_effect = RuntimeError("network down")
        items = catalog.list_inter_assist_countries(client=client)
        self.assertTrue(any(item["id"] == "AR" for item in items))
        self.assertGreaterEqual(len(items), 1)

    def test_ar_provinces_from_georef(self) -> None:
        client = MagicMock()
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {
            "provincias": [
                {"id": "14", "nombre": "Córdoba"},
                {"id": "06", "nombre": "Buenos Aires"},
            ]
        }
        response.raise_for_status = MagicMock()
        client.get.return_value = response

        items = catalog.list_inter_assist_provinces("AR", client=client)
        self.assertEqual(
            items,
            [
                {"id": "06", "name": "Buenos Aires"},
                {"id": "14", "name": "Córdoba"},
            ],
        )

    def test_other_country_has_no_provinces(self) -> None:
        items = catalog.list_inter_assist_provinces("BR")
        self.assertEqual(items, [])

    def test_ar_provinces_fallback(self) -> None:
        client = MagicMock()
        client.get.side_effect = RuntimeError("georef down")
        items = catalog.list_inter_assist_provinces("ar", client=client)
        self.assertEqual(len(items), 24)
        self.assertTrue(any(item["id"] == "06" for item in items))
