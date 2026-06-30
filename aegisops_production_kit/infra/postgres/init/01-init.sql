-- AegisOps Postgres bootstrap. Runs ONCE on first cluster init (empty volume).
-- Creates the pgvector extension in the app DB and a dedicated database for Langfuse.

-- pgvector for RAG embeddings (document_chunks.embedding).
CREATE EXTENSION IF NOT EXISTS vector;
-- pg_trgm helps text/keyword filtering alongside vector search.
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- Langfuse v2 owns its own schema/migrations; it just needs the database to exist.
SELECT 'CREATE DATABASE langfuse'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'langfuse')\gexec
