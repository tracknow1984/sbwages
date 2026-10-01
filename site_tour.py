"""Admin-only aerial tour using the owner's supplied site screenshots."""
from pathlib import Path
from flask import abort, render_template, send_file

VIEWS = [
    {'id': 'overview', 'title': 'Site overview', 'file': 'lot3-overview.jpg', 'description': 'Overview of the supplied Lot 3 aerial imagery.'},
    {'id': 'buildings', 'title': 'Buildings & yard', 'file': 'lot3-buildings.jpg', 'description': 'Closer view of the buildings and adjacent yard.'},
    {'id': 'yard', 'title': 'Yard detail', 'file': 'lot3-yard.jpg', 'description': 'Closer view of the central yard and access track.'},
    {'id': 'edge', 'title': 'Site edge', 'file': 'lot3-edge.jpg', 'description': 'Closer view of the vegetated edge and track.'},
]

def register_site_tour(app, require):
    @app.get('/admin/site-tour')
    @require('admin')
    def admin_site_tour():
        return render_template('site_tour.html', tab='site-tour', views=VIEWS)

    @app.get('/admin/site-tour/media/<view_id>')
    @require('admin')
    def site_tour_media(view_id):
        view = next((v for v in VIEWS if v['id'] == view_id), None)
        if view is None:
            abort(404)
        return send_file(Path(app.root_path) / 'site_tour_media' / view['file'], mimetype='image/jpeg', conditional=True, max_age=86400)
