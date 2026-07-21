from unittest.mock import patch

from django.test import SimpleTestCase

from bots import erp_return_utils


class ErpReturnUtilsTest(SimpleTestCase):
    def test_template_context_uses_module_defaults(self):
        context = erp_return_utils.erp_return_template_context()

        self.assertEqual(
            context["erp_notulen_list_url"],
            "https://alpha.boga.co.id/WebAppsAlpha/Transactions/NotulenMeeting.aspx",
        )
        self.assertEqual(context["erp_parent_origin"], "https://alpha.boga.co.id")
        self.assertFalse(context["erp_return_always"])

    @patch.object(erp_return_utils, "ERP_NOTULEN_LIST_URL", "https://example.test/notulen")
    @patch.object(erp_return_utils, "ERP_PARENT_ORIGIN", "https://example.test")
    @patch.object(erp_return_utils, "ERP_RETURN_ALWAYS", True)
    def test_template_context_reflects_module_settings(self):
        context = erp_return_utils.erp_return_template_context()

        self.assertEqual(context["erp_notulen_list_url"], "https://example.test/notulen")
        self.assertEqual(context["erp_parent_origin"], "https://example.test")
        self.assertTrue(context["erp_return_always"])
