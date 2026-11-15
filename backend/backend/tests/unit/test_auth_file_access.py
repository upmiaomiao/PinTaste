import bcrypt

from middleware.auth import generate_token


def test_project_files_require_ownership(app):
    from models import db, Project, User

    with app.app_context():
        owner = User(username='owner_user', password_hash=bcrypt.hashpw(b'pw', bcrypt.gensalt()).decode('utf-8'), role='employee')
        other = User(username='other_user', password_hash=bcrypt.hashpw(b'pw', bcrypt.gensalt()).decode('utf-8'), role='employee')
        project = Project(id='project-files-1', creation_type='idea', idea_prompt='test', user_id='owner-id')
        owner.id = 'owner-id'
        other.id = 'other-id'
        db.session.add_all([owner, other, project])
        db.session.commit()

        exports_dir = app.config['UPLOAD_FOLDER']

    import os
    project_exports = os.path.join(app.config['UPLOAD_FOLDER'], 'project-files-1', 'exports')
    os.makedirs(project_exports, exist_ok=True)
    target = os.path.join(project_exports, 'deck.pptx')
    with open(target, 'wb') as handle:
        handle.write(b'test')

    with app.test_client() as client:
        other_token = generate_token('other-id', 'employee')
        response = client.get('/files/project-files-1/exports/deck.pptx', headers={'Authorization': f'Bearer {other_token}'})
        assert response.status_code == 403

        owner_token = generate_token('owner-id', 'employee')
        response = client.get('/files/project-files-1/exports/deck.pptx', headers={'Authorization': f'Bearer {owner_token}'})
        assert response.status_code == 200
        assert response.data == b'test'
