"""
Management command: requery_dva

Usage examples:
  python manage.py requery_dva 9880072020
  python manage.py requery_dva 9880072020 --provider-slug wema-bank
  python manage.py requery_dva 9880072020 --date 2026-04-12
  python manage.py requery_dva 9880072020 --check-user
"""

from django.core.management.base import BaseCommand
from django.contrib.auth import get_user_model
from wallet.models import Wallet, Transaction
from wallet.utils import PaystackClient
import logging

logger = logging.getLogger(__name__)
User = get_user_model()


class Command(BaseCommand):
    help = (
        "Requery a Paystack Dedicated Virtual Account (DVA) for unprocessed transfers. "
        "Paystack will re-fire the charge.success webhook if a pending transfer is found."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            'account_number',
            type=str,
            help='The DVA bank account number to requery (e.g. 9880072020)',
        )
        parser.add_argument(
            '--provider-slug',
            type=str,
            default='titan-paystack',
            dest='provider_slug',
            help='Paystack bank provider slug (default: titan-paystack)',
        )
        parser.add_argument(
            '--date',
            type=str,
            default=None,
            help='Filter by transfer date in YYYY-MM-DD format (optional)',
        )
        parser.add_argument(
            '--check-user',
            action='store_true',
            default=False,
            dest='check_user',
            help='Only check the user record — do not call Paystack API',
        )

    def handle(self, *args, **options):
        account_number = options['account_number']
        provider_slug  = options['provider_slug']
        date_filter    = options['date']
        check_user_only = options['check_user']

        self.stdout.write(self.style.HTTP_INFO(
            f"\n{'='*60}\n"
            f"  DVA Requery Diagnostic Tool\n"
            f"  Account: {account_number}  Provider: {provider_slug}\n"
            f"{'='*60}\n"
        ))

        # ── Step 1: User / wallet check ───────────────────────────────────────
        self.stdout.write(self.style.HTTP_INFO("[ STEP 1 ] Looking up user by virtual_account_number …"))
        user = User.objects.filter(virtual_account_number=account_number).first()

        if user:
            self.stdout.write(self.style.SUCCESS(
                f"  ✓ User found: {user.email} (pk={user.pk})"
            ))
            self.stdout.write(f"    paystack_customer_code : {user.paystack_customer_code or '(not set)'}")
            self.stdout.write(f"    virtual_bank_name      : {user.virtual_bank_name or '(not set)'}")
            self.stdout.write(f"    virtual_account_name   : {user.virtual_account_name or '(not set)'}")

            # Wallet balance
            wallet = Wallet.objects.filter(user_id=user.id).first()
            if wallet:
                self.stdout.write(f"    wallet balance         : ₦{wallet.balance:,.2f}")
            else:
                self.stdout.write(self.style.WARNING("    wallet                 : NOT FOUND"))

            # Last 5 transactions
            if wallet:
                recent_txs = (
                    Transaction.objects
                    .filter(wallet=wallet)
                    .order_by('-created_at')[:5]
                )
                if recent_txs:
                    self.stdout.write("\n    Last 5 transactions:")
                    for tx in recent_txs:
                        self.stdout.write(
                            f"      [{tx.status:10}] ₦{tx.amount:>12,.2f}  "
                            f"{tx.transaction_type:12}  ref={tx.reference}  "
                            f"ext={tx.external_reference or '—'}  {tx.created_at:%Y-%m-%d %H:%M}"
                        )
                else:
                    self.stdout.write("    No transactions found.")
        else:
            self.stdout.write(self.style.ERROR(
                f"  ✗ No user found with virtual_account_number='{account_number}'."
            ))
            all_duas = list(
                User.objects
                .exclude(virtual_account_number__isnull=True)
                .exclude(virtual_account_number='')
                .values_list('virtual_account_number', 'email')
            )
            if all_duas:
                self.stdout.write(
                    f"\n  All stored DVA numbers in the system ({len(all_duas)} total):"
                )
                for dva_num, email in all_duas:
                    self.stdout.write(f"    {dva_num}  →  {email}")
            else:
                self.stdout.write("  No users with a virtual_account_number exist in the DB.")

        if check_user_only:
            self._print_checklist(account_number)
            return

        # ── Step 2: Call Paystack requery API ─────────────────────────────────
        self.stdout.write(self.style.HTTP_INFO(
            f"\n[ STEP 2 ] Calling Paystack DVA requery API …"
        ))
        params = {
            'account_number': account_number,
            'provider_slug':  provider_slug,
        }
        if date_filter:
            params['date'] = date_filter
            self.stdout.write(f"  Date filter: {date_filter}")

        try:
            client = PaystackClient()
            result = client._request('GET', '/dedicated_account/requery', params)
            if result:
                self.stdout.write(self.style.SUCCESS(
                    f"  ✓ Paystack responded: {result}"
                ))
                # Paystack returns {"status": true, "message": "..."}
                msg = result.get('message', '')
                if result.get('status'):
                    self.stdout.write(self.style.SUCCESS(
                        f"\n  Paystack will re-fire the charge.success webhook if there are "
                        f"unprocessed transfers for this DVA.\n"
                        f"  Wait ~30 seconds and check your production logs."
                    ))
                else:
                    self.stdout.write(self.style.WARNING(
                        f"  Paystack returned status=false: {msg}"
                    ))
            else:
                self.stdout.write(self.style.WARNING(
                    "  Paystack returned an empty response."
                ))
        except Exception as e:
            self.stdout.write(self.style.ERROR(f"  ✗ Paystack API error: {e}"))

        # ── Diagnostic checklist ──────────────────────────────────────────────
        self._print_checklist(account_number)

    def _print_checklist(self, account_number):
        self.stdout.write(self.style.HTTP_INFO(
            f"\n{'='*60}\n"
            f"  DIAGNOSTIC CHECKLIST\n"
            f"{'='*60}"
        ))
        checks = [
            ("Paystack Dashboard → Settings → Webhooks",
             "Verify the URL is https://api.midman.app/webhooks/paystack/ "
             "and is ACTIVE. Test it by clicking 'Send test event'."),

            ("Paystack Dashboard → Transactions",
             f"Search for transfers to account {account_number}. "
             "Confirm the transfer shows as 'Success'."),

            ("Paystack Dashboard → Settings → Webhook Logs",
             "Look for failed webhook deliveries for charge.success events. "
             "A 400 response means signature verification failed "
             "(check PAYSTACK_SECRET_KEY env var)."),

            ("Django Production Logs (after logging fix)",
             "Look for '🔍 DIAG-' prefixed lines. "
             "DIAG-01 means the webhook reached Django. "
             "DIAG-08/09/10 will show which user lookup failed. "
             "DIAG-11 means all lookups failed — the DVA number mismatch is the root cause."),

            ("User model fields to verify",
             f"Run: python manage.py requery_dva {account_number} --check-user\n"
             "     Confirm virtual_account_number, paystack_customer_code, and email "
             "match what Paystack sends in the webhook payload."),
        ]
        for i, (title, detail) in enumerate(checks, 1):
            self.stdout.write(f"\n  ({i}) {title}")
            self.stdout.write(f"      {detail}")
        self.stdout.write("")
