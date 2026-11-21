import sys
import warnings
from getpass import GetPassWarning, getpass

from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.views.decorators.debug import sensitive_variables


class Command(BaseCommand):
    help = "Interactively create the first administrator without accepting a password via arguments or environment."
    requires_migrations_checks = True

    def add_arguments(self, parser):
        parser.add_argument("username", help="Username for the first administrator")

    @sensitive_variables("password", "confirmation")
    def handle(self, *args, **options):
        if not sys.stdin.isatty():
            raise CommandError("An interactive terminal is required.")

        User = get_user_model()
        user = User(username=User.normalize_username(options["username"]), is_staff=True, is_superuser=True)
        try:
            user.full_clean(exclude=["password"])
        except ValidationError as error:
            raise CommandError("; ".join(error.messages)) from None
        if User.objects.filter(is_superuser=True).exists():
            raise CommandError("An administrator already exists.")

        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", GetPassWarning)
                password = getpass("Administrator password: ")
                confirmation = getpass("Confirm password: ")
        except (EOFError, KeyboardInterrupt, GetPassWarning):
            raise CommandError("Password entry cancelled or hidden input unavailable.") from None
        if password != confirmation:
            raise CommandError("Passwords do not match.")
        try:
            validate_password(password, user)
        except ValidationError as error:
            raise CommandError("; ".join(error.messages)) from None

        user.set_password(password)
        with transaction.atomic():
            if User.objects.filter(is_superuser=True).exists() or User.objects.filter(username=user.username).exists():
                raise CommandError("An administrator or that username already exists.")
            user.save(force_insert=True)
        self.stdout.write(self.style.SUCCESS("Initial administrator created."))
