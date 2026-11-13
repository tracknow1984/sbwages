import io
import re
import secrets

from flask import abort, flash, g, redirect, render_template, request, send_file, url_for

from document_uploads import MAX_STAFF_FILES, read_document


SCHEMA = """
CREATE TABLE IF NOT EXISTS company_document_folders (
    id INTEGER PRIMARY KEY, name TEXT NOT NULL COLLATE NOCASE UNIQUE
);
CREATE TABLE IF NOT EXISTS company_documents (
    id INTEGER PRIMARY KEY, folder_id INTEGER NOT NULL REFERENCES company_document_folders(id),
    filename TEXT NOT NULL, mime_type TEXT NOT NULL, data BLOB NOT NULL,
    uploaded_at TEXT NOT NULL, uploaded_by INTEGER NOT NULL REFERENCES users(id),
    batch_key TEXT NOT NULL, batch_position INTEGER NOT NULL,
    UNIQUE(folder_id, uploaded_by, batch_key, batch_position)
);
CREATE INDEX IF NOT EXISTS company_documents_by_folder ON company_documents(folder_id, id DESC);
"""


def register(app, db, require, timestamp, document_rows):
    def folder_or_404(folder_id):
        folder = db().execute('SELECT * FROM company_document_folders WHERE id=?', (folder_id,)).fetchone()
        if not folder:
            abort(404)
        return folder

    def folder_url(folder_id):
        return url_for('admin_document_folder', folder_id=folder_id)

    @app.get('/admin/documents')
    @require('admin')
    def admin_documents():
        archived = request.args.get('archived') == '1'
        folders = db().execute("""
            SELECT f.id,f.name,count(d.id) AS total FROM company_document_folders f
            LEFT JOIN company_documents d ON d.folder_id=f.id GROUP BY f.id ORDER BY f.name
        """).fetchall()
        return render_template('company_documents.html', folders=folders, folder=None, archived=archived,
                               documents=document_rows(archived=archived), admin_view=True, tab='documents')

    @app.post('/admin/documents/folders')
    @require('admin')
    def create_document_folder():
        name = request.form.get('name', '').strip()
        if not name or len(name) > 100:
            flash('Enter a folder name of 1 to 100 characters.', 'error')
            return redirect(url_for('admin_documents'), code=303)
        with db() as conn:
            conn.execute('INSERT INTO company_document_folders(name) VALUES(?) ON CONFLICT(name) DO NOTHING', (name,))
            folder_id = conn.execute('SELECT id FROM company_document_folders WHERE name=?', (name,)).fetchone()[0]
        return redirect(folder_url(folder_id), code=303)

    @app.get('/admin/documents/folders/<int:folder_id>')
    @require('admin')
    def admin_document_folder(folder_id):
        folder = folder_or_404(folder_id)
        files = db().execute("""
            SELECT id,filename,mime_type,uploaded_at,length(data) AS size FROM company_documents
            WHERE folder_id=? ORDER BY id DESC
        """, (folder_id,)).fetchall()
        return render_template('company_documents.html', folder=folder, files=files, tab='documents',
                               upload_key=secrets.token_urlsafe(24), max_files=MAX_STAFF_FILES,
                               size_mb=app.config['DOCUMENT_MAX_BYTES'] // (1024 * 1024))

    @app.post('/admin/documents/folders/<int:folder_id>/upload')
    @require('admin')
    def upload_company_documents(folder_id):
        folder_or_404(folder_id)
        key = request.form.get('upload_key', '')
        if not re.fullmatch(r'[A-Za-z0-9_-]{20,80}', key):
            abort(400, 'Refresh this folder and select your files again.')
        uploads = request.files.getlist('document')
        uploader = g.user['id']
        try:
            if not 1 <= len(uploads) <= MAX_STAFF_FILES:
                raise ValueError(f'Select between 1 and {MAX_STAFF_FILES} files.')
            conn = db()
            with conn:
                conn.execute('BEGIN IMMEDIATE')
                if conn.execute('SELECT 1 FROM company_documents WHERE folder_id=? AND uploaded_by=? AND batch_key=? LIMIT 1',
                                (folder_id, uploader, key)).fetchone():
                    return redirect(folder_url(folder_id), code=303)
                stamp = timestamp()
                for position, upload in enumerate(uploads):
                    filename, mime, data = read_document(upload, app.config['DOCUMENT_MAX_BYTES'], verify_contents=True)
                    conn.execute("""INSERT INTO company_documents
                        (folder_id,filename,mime_type,data,uploaded_at,uploaded_by,batch_key,batch_position)
                        VALUES(?,?,?,?,?,?,?,?)""", (folder_id,filename,mime,data,stamp,uploader,key,position))
            flash(f'Uploaded {len(uploads)} file' + ('s.' if len(uploads) != 1 else '.'), 'success')
        except ValueError as exc:
            flash(f'{exc} No files were saved.', 'error')
        return redirect(folder_url(folder_id), code=303)

    @app.get('/admin/documents/folders/<int:folder_id>/files/<int:file_id>')
    @require('admin')
    def company_document_file(folder_id, file_id):
        file = db().execute('SELECT filename,mime_type,data FROM company_documents WHERE id=? AND folder_id=?',
                            (file_id, folder_id)).fetchone()
        if not file:
            abort(404)
        return send_file(io.BytesIO(file['data']), download_name=file['filename'], mimetype=file['mime_type'],
                         as_attachment=request.args.get('download') == '1', max_age=0)
