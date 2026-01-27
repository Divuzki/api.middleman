from django.apps import AppConfig


class WagerConfig(AppConfig):
    name = 'wager'

    def ready(self):
        import wager.signals
