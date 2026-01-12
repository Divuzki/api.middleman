class WalletRouter:
    """
    A router to control all database operations on models in the
    wallet application.
    """
    route_app_labels = {'wallet'}

    def db_for_read(self, model, **hints):
        """
        Attempts to read wallet models go to wallet_db.
        """
        if model._meta.app_label in self.route_app_labels:
            return 'wallet_db'
        return None

    def db_for_write(self, model, **hints):
        """
        Attempts to write wallet models go to wallet_db.
        """
        if model._meta.app_label in self.route_app_labels:
            return 'wallet_db'
        return None

    def allow_relation(self, obj1, obj2, **hints):
        """
        Allow relations if a model in the wallet app is involved.
        Relations between different databases are generally allowed by Django
        but ForeignKeys must be handled carefully (no integrity checks).
        """
        if (
            obj1._meta.app_label in self.route_app_labels or
            obj2._meta.app_label in self.route_app_labels
        ):
           return True
        return None

    def allow_migrate(self, db, app_label, model_name=None, **hints):
        """
        Make sure the wallet app only appears in the 'wallet_db'
        database.
        """
        if app_label in self.route_app_labels:
            return db == 'wallet_db'
        return None
