from django.contrib.auth.models import AnonymousUser
from django.test import TestCase

from accounts.models import Organization, User, UserRole
from bots.meeting_summary_guest_utils import user_can_share_guest_mom_link
from bots.models import Project, ProjectAccess


class UserCanShareGuestMomLinkTest(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Org", centicredits=10000)
        self.project = Project.objects.create(name="Proj", organization=self.organization)
        self.admin = User.objects.create_user(
            username="admin",
            email="admin@example.com",
            password="x",
            role=UserRole.ADMIN,
        )
        self.admin.organization = self.organization
        self.admin.save()
        self.member = User.objects.create_user(
            username="member",
            email="member@example.com",
            password="x",
            role=UserRole.REGULAR_USER,
        )
        self.member.organization = self.organization
        self.member.save()
        ProjectAccess.objects.create(project=self.project, user=self.member)
        self.outsider = User.objects.create_user(
            username="outsider",
            email="outsider@example.com",
            password="x",
            role=UserRole.REGULAR_USER,
        )
        self.outsider.organization = self.organization
        self.outsider.save()

    def test_admin_may_share_without_project_access_row(self):
        self.assertTrue(user_can_share_guest_mom_link(self.admin, self.project))

    def test_member_with_project_access_may_share(self):
        self.assertTrue(user_can_share_guest_mom_link(self.member, self.project))

    def test_regular_user_without_project_access_may_not_share(self):
        self.assertFalse(user_can_share_guest_mom_link(self.outsider, self.project))

    def test_anonymous_may_not_share(self):
        self.assertFalse(user_can_share_guest_mom_link(AnonymousUser(), self.project))
