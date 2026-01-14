class AuthRouter:
    """
    A router to control all database operations on models in the
    auth, contenttypes, sessions, admin, and users applications.
    """
    route_app_labels = {'auth', 'contenttypes', 'sessions', 'admin', 'users', 'messages', 'staticfiles'}

    def db_for_read(self, model, **hints):
        """
        Attempts to read auth models go to default.
        """
        if model._meta.app_label in self.route_app_labels:
            return 'default'
        return None

    def db_for_write(self, model, **hints):
        """
        Attempts to write auth models go to default.
        """
        if model._meta.app_label in self.route_app_labels:
            return 'default'
        return None

    def allow_relation(self, obj1, obj2, **hints):
        """
        Allow relations if a model in the auth app is involved.
        """
        if (
            obj1._meta.app_label in self.route_app_labels or
            obj2._meta.app_label in self.route_app_labels
        ):
           return True
        return None

    def allow_migrate(self, db, app_label, model_name=None, **hints):
        """
        Make sure the auth apps only appear in the 'default' database.
        """
        if app_label in self.route_app_labels:
            return db == 'default'
        return None


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
        # print(f"DEBUG: allow_migrate db={db} app={app_label}")
        if app_label in self.route_app_labels:
            return db == 'wallet_db'
        
        # Prevent other apps from migrating to wallet_db
        if db == 'wallet_db':
            return False
            
        return None


class WagerRouter:
    """
    A router to control all database operations on models in the
    wager application.
    """
    route_app_labels = {'wager'}

    def db_for_read(self, model, **hints):
        """
        Attempts to read wager models go to wager_db.
        """
        if model._meta.app_label in self.route_app_labels:
            return 'wager_db'
        return None

    def db_for_write(self, model, **hints):
        """
        Attempts to write wager models go to wager_db.
        """
        if model._meta.app_label in self.route_app_labels:
            return 'wager_db'
        return None

    def allow_relation(self, obj1, obj2, **hints):
        """
        Allow relations if a model in the wager app is involved.
        """
        if (
            obj1._meta.app_label in self.route_app_labels or
            obj2._meta.app_label in self.route_app_labels
        ):
           return True
        return None

    def allow_migrate(self, db, app_label, model_name=None, **hints):
        """
        Make sure the wager app only appears in the 'wager_db'
        database.
        """
        if app_label in self.route_app_labels:
            return db == 'wager_db'
        
        # Prevent other apps from migrating to wager_db
        if db == 'wager_db':
            return False
            
        return None


class AgreementRouter:
    """
    A router to control all database operations on models in the
    agreement application.
    """
    route_app_labels = {'agreement'}

    def db_for_read(self, model, **hints):
        """
        Attempts to read agreement models go to agreement_db.
        """
        if model._meta.app_label in self.route_app_labels:
            return 'agreement_db'
        return None

    def db_for_write(self, model, **hints):
        """
        Attempts to write agreement models go to agreement_db.
        """
        if model._meta.app_label in self.route_app_labels:
            return 'agreement_db'
        return None

    def allow_relation(self, obj1, obj2, **hints):
        """
        Allow relations if a model in the agreement app is involved.
        """
        if (
            obj1._meta.app_label in self.route_app_labels or
            obj2._meta.app_label in self.route_app_labels
        ):
           return True
        return None

    def allow_migrate(self, db, app_label, model_name=None, **hints):
        """
        Make sure the agreement app only appears in the 'agreement_db'
        database.
        """
        if app_label in self.route_app_labels:
            return db == 'agreement_db'
        
        # Prevent other apps from migrating to agreement_db
        if db == 'agreement_db':
            return False
            
        return None
