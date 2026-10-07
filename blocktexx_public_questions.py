"""Public questions only; replies remain private to their browser and administrators."""
import json
import re
import secrets
from datetime import datetime, timezone
from pathlib import Path
from flask import request, session, render_template, redirect, url_for, g

def question_bank():
    source = (Path(__file__).parent / 'static' / 'blocktexx_questions.js').read_text()
    return json.loads(re.search(r'const bank=(\[.*?\]);', source, re.S).group(1))

def register_public_questions(app, db, require):
    bank = question_bank()
    known = {q['id']: q for q in bank}
    with app.app_context():
        db().execute('''CREATE TABLE IF NOT EXISTS blocktexx_public_replies (
            id TEXT PRIMARY KEY, respondent TEXT NOT NULL, email TEXT NOT NULL,
            organisation TEXT NOT NULL, answers TEXT NOT NULL, status TEXT NOT NULL,
            revision INTEGER NOT NULL, updated_at TEXT NOT NULL, submitted_at TEXT,
            applied_at TEXT)''')
        db().commit()

    @app.get('/blocktexx/questions')
    def public_blocktexx_questions():
        response_id = session.setdefault('blocktexx_reply', secrets.token_urlsafe(32))
        row = db().execute('SELECT * FROM blocktexx_public_replies WHERE id=?', (response_id,)).fetchone()
        data = {'respondent': '', 'email': '', 'organisation': '', 'answers': {}, 'revision': 0, 'status': 'draft'}
        if row:
            data.update({key: row[key] for key in ('respondent', 'email', 'organisation', 'revision', 'status')})
            data['answers'] = json.loads(row['answers'])
        return render_template('blocktexx_public_questions.html', data=data, count=len(bank))

    @app.post('/blocktexx/questions/save')
    def save_public_blocktexx_questions():
        response_id = session.get('blocktexx_reply')
        if not response_id:
            return {'error': 'Open the questions page before saving.'}, 400
        try:
            payload = json.loads(request.form.get('reply', '{}'))
            if not isinstance(payload, dict) or set(payload) - {'respondent', 'email', 'organisation', 'answers', 'revision', 'submit'}:
                raise ValueError('Invalid response fields.')
            answers = payload.get('answers', {})
            if not isinstance(answers, dict) or len(answers) > len(bank):
                raise ValueError('Invalid question answers.')
            cleaned = {}
            for key, value in answers.items():
                if key not in known or not isinstance(value, dict) or set(value) - {'answer', 'status'}:
                    raise ValueError('Unknown question or answer fields.')
                answer, status = value.get('answer', ''), value.get('status', 'open')
                if not isinstance(answer, str) or len(answer) > 3000 or status not in ('open', 'awaiting', 'answered', 'not_applicable'):
                    raise ValueError('Answers must be at most 3000 characters with a valid status.')
                cleaned[key] = {'answer': answer.strip(), 'status': status}
            fields = {}
            for key, limit in (('respondent', 120), ('email', 254), ('organisation', 160)):
                value = payload.get(key, '')
                if not isinstance(value, str) or len(value) > limit:
                    raise ValueError('Check the respondent details.')
                fields[key] = value.strip()
            if fields['email'] and not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', fields['email']):
                raise ValueError('Enter a valid email address.')
            submit = payload.get('submit') is True
            if submit and (not fields['respondent'] or not fields['email']):
                raise ValueError('Enter your name and email before submitting.')
            if submit and not any(a['answer'] or a['status'] == 'not_applicable' for a in cleaned.values()):
                raise ValueError('Answer at least one question before submitting.')
            revision = payload.get('revision', 0)
            if type(revision) is not int or revision < 0:
                raise ValueError('Invalid response version.')
        except (ValueError, TypeError, AttributeError) as exc:
            return {'error': str(exc)}, 400
        db().execute('BEGIN IMMEDIATE')
        old = db().execute('SELECT revision FROM blocktexx_public_replies WHERE id=?', (response_id,)).fetchone()
        if revision != (old['revision'] if old else 0):
            db().rollback()
            return {'error': 'This response was saved in another tab. Refresh before making further changes.'}, 409
        now = datetime.now(timezone.utc).isoformat()
        status = 'submitted' if submit else 'draft'
        db().execute('''INSERT INTO blocktexx_public_replies
            (id,respondent,email,organisation,answers,status,revision,updated_at,submitted_at,applied_at)
            VALUES(?,?,?,?,?,?,?,?,?,NULL) ON CONFLICT(id) DO UPDATE SET
            respondent=excluded.respondent,email=excluded.email,organisation=excluded.organisation,
            answers=excluded.answers,status=excluded.status,revision=excluded.revision,
            updated_at=excluded.updated_at,submitted_at=excluded.submitted_at,applied_at=NULL''',
            (response_id, fields['respondent'], fields['email'], fields['organisation'], json.dumps(cleaned), status, revision + 1, now, now if submit else None))
        db().commit()
        return {'ok': True, 'revision': revision + 1, 'status': status, 'saved': now}

    @app.get('/admin/blocktexx/responses')
    @require('admin')
    def blocktexx_public_responses():
        replies = []
        for row in db().execute('SELECT * FROM blocktexx_public_replies ORDER BY updated_at DESC').fetchall():
            item = dict(row)
            item['answers'] = [{'question': known[key]['question'], **answer} for key, answer in json.loads(row['answers']).items() if key in known and (answer.get('answer') or answer.get('status') == 'not_applicable')]
            replies.append(item)
        return render_template('blocktexx_public_responses.html', replies=replies)

    @app.post('/admin/blocktexx/responses/<response_id>/apply')
    @require('admin')
    def apply_blocktexx_public_response(response_id):
        from blocktexx import empty_model, validate_model
        db().execute('BEGIN IMMEDIATE')
        reply = db().execute('SELECT * FROM blocktexx_public_replies WHERE id=?', (response_id,)).fetchone()
        if not reply or reply['status'] != 'submitted':
            db().rollback()
            return {'error': 'Submitted response not found.'}, 404
        old = db().execute('SELECT * FROM blocktexx_model WHERE id=1').fetchone()
        model = validate_model(json.loads(old['data'])) if old else validate_model(empty_model())
        for key, answer in json.loads(reply['answers']).items():
            if key in known and (answer['answer'] or answer['status'] == 'not_applicable'):
                model['clarification_answers'][key] = answer
        payload = json.dumps(validate_model(model), allow_nan=False)
        revision, now = (old['revision'] if old else 0) + 1, datetime.now(timezone.utc).isoformat()
        db().execute('INSERT OR REPLACE INTO blocktexx_model VALUES(1,?,?,?,?)', (revision, payload, g.user['id'], now))
        db().execute('INSERT INTO blocktexx_model_history VALUES(?,?,?,?)', (revision, payload, g.user['id'], now))
        db().execute('UPDATE blocktexx_public_replies SET applied_at=? WHERE id=?', (now, response_id))
        db().commit()
        return redirect(url_for('blocktexx_public_responses', applied=1))
