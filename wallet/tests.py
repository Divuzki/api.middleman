from django.test import SimpleTestCase
from django.urls import resolve
from .views import DepositView, VerifyDepositView

class UrlRoutingTests(SimpleTestCase):
    def test_verify_deposit_url_resolves_correctly(self):
        # This URL should resolve to VerifyDepositView
        url = '/users/deposit/verify/ref_cbb7075548e0/'
        resolver = resolve(url)
        self.assertEqual(resolver.func.view_class, VerifyDepositView)
        self.assertEqual(resolver.kwargs['reference'], 'ref_cbb7075548e0')

    def test_deposit_url_resolves_correctly(self):
        url = '/users/deposit/'
        resolver = resolve(url)
        self.assertEqual(resolver.func.view_class, DepositView)
