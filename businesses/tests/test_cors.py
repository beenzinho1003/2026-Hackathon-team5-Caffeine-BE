from django.test import TestCase


class CorsConfigurationTests(TestCase):
    def test_vite_development_origin_is_allowed_for_api(self):
        response = self.client.options(
            "/api/businesses/1/",
            HTTP_ORIGIN="http://localhost:5173",
            HTTP_ACCESS_CONTROL_REQUEST_METHOD="GET",
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(
            response.headers.get("Access-Control-Allow-Origin", ""),
            ["http://localhost:5173", "*"],
        )
