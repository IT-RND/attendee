from django.http import Http404
from django.shortcuts import redirect

from bots.models import Project


def home(request):
    if not request.user.is_authenticated:
        return redirect("projects:guest-create-session")

    # Get the first bot for the user
    project = Project.accessible_to(request.user).first()
    if not project:
        project = Project.objects.create(
            name=f"{request.user.email}'s project",
            organization=request.user.organization,
        )
    if project:
        return redirect("projects:project-dashboard", object_id=project.object_id)
    raise Http404("No projects found for this organization. You need to create a project first.")
