CREATE TABLE works(id TEXT PRIMARY KEY,title TEXT NOT NULL,created_at TEXT NOT NULL);
ALTER TABLE projects ADD COLUMN work_id TEXT REFERENCES works(id);
ALTER TABLE projects ADD COLUMN lifecycle TEXT NOT NULL DEFAULT 'active' CHECK(lifecycle IN ('active','completed','archived'));
INSERT INTO works SELECT id,title,created_at FROM projects;
UPDATE projects SET work_id=id;
CREATE INDEX projects_work ON projects(work_id,created_at);
CREATE TRIGGER project_default_work AFTER INSERT ON projects WHEN NEW.work_id IS NULL BEGIN
 INSERT INTO works(id,title,created_at) VALUES(NEW.id,NEW.title,NEW.created_at);
 UPDATE projects SET work_id=NEW.id WHERE id=NEW.id;
END;
CREATE TABLE input_imports(id TEXT PRIMARY KEY,project_id TEXT NOT NULL REFERENCES projects(id),manifest_json TEXT NOT NULL,files_json TEXT NOT NULL,created_at TEXT NOT NULL);
