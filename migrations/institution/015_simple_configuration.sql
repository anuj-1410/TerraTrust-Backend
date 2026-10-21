-- Editable inputs are separate from immutable, accepted calculation history.
CREATE TABLE institution_calculation_settings (
 id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 input_revision INTEGER NOT NULL DEFAULT 1 CHECK(input_revision>0),
 data_origin TEXT NOT NULL DEFAULT 'LIVE' CHECK(data_origin='LIVE'),
 logical_key TEXT NOT NULL UNIQUE CHECK(logical_key='CURRENT'),
 config JSONB NOT NULL DEFAULT '{}'::jsonb CHECK(jsonb_typeof(config)='object')
);
INSERT INTO institution_calculation_settings(logical_key,config)
 VALUES('CURRENT',COALESCE((SELECT config FROM carbon_methodologies WHERE status='APPROVED' AND data_origin='LIVE' ORDER BY validated_at DESC NULLS LAST,created_at DESC,id DESC LIMIT 1),'{}'::jsonb));
CREATE INDEX tt_current_compatible_corpus ON knowledge_corpus_versions
 ((embedding_config->>'model'),published_at DESC NULLS LAST,created_at DESC,id DESC)
 WHERE status='ACTIVE' AND embedding_config->>'provider'='GEMINI';

CREATE TABLE institution_formula_values (
 id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 input_revision INTEGER NOT NULL DEFAULT 1 CHECK(input_revision>0),
 data_origin TEXT NOT NULL DEFAULT 'LIVE' CHECK(data_origin='LIVE'),
 logical_key TEXT NOT NULL UNIQUE,
 value NUMERIC(24,12) CHECK(value>=0 AND value<>'NaN'::numeric),
 config JSONB NOT NULL DEFAULT '{}'::jsonb CHECK(jsonb_typeof(config)='object'),
 source_label TEXT NOT NULL,
 geography_codes TEXT[] NOT NULL DEFAULT '{}',
 valid_from DATE,
 valid_to DATE,
 enabled BOOLEAN NOT NULL DEFAULT false,
 CHECK(NOT enabled OR value IS NOT NULL),
 CHECK(valid_to IS NULL OR valid_from IS NULL OR valid_to>=valid_from)
);

CREATE TABLE institution_knowledge_sources (
 id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 input_revision INTEGER NOT NULL DEFAULT 1 CHECK(input_revision>0),
 data_origin TEXT NOT NULL DEFAULT 'LIVE' CHECK(data_origin='LIVE'),
 logical_key TEXT NOT NULL UNIQUE,
 title TEXT NOT NULL,
 publisher TEXT NOT NULL,
 object_key TEXT NOT NULL CHECK(object_key LIKE 'knowledge/%' AND object_key NOT LIKE '%..%'),
 license TEXT NOT NULL,
 source_url TEXT,
 geography_codes TEXT[] NOT NULL DEFAULT '{}',
 topic_codes TEXT[] NOT NULL DEFAULT '{}',
 language TEXT NOT NULL DEFAULT 'en',
 valid_from DATE,
 valid_to DATE,
 enabled BOOLEAN NOT NULL DEFAULT false,
 public_licensed BOOLEAN NOT NULL DEFAULT false,
 CHECK(NOT enabled OR public_licensed),
 CHECK(valid_to IS NULL OR valid_from IS NULL OR valid_to>=valid_from)
);

CREATE FUNCTION tt_simple_configuration_revision() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 NEW.updated_at=now(); NEW.input_revision=OLD.input_revision+1; RETURN NEW;
END $$;
REVOKE ALL ON FUNCTION tt_simple_configuration_revision() FROM PUBLIC;
CREATE TRIGGER institution_calculation_settings_revision BEFORE UPDATE ON institution_calculation_settings FOR EACH ROW EXECUTE FUNCTION tt_simple_configuration_revision();
CREATE TRIGGER institution_formula_values_revision BEFORE UPDATE ON institution_formula_values FOR EACH ROW EXECUTE FUNCTION tt_simple_configuration_revision();
CREATE TRIGGER institution_knowledge_sources_revision BEFORE UPDATE ON institution_knowledge_sources FOR EACH ROW EXECUTE FUNCTION tt_simple_configuration_revision();

ALTER TABLE institution_calculation_settings ENABLE ROW LEVEL SECURITY;
ALTER TABLE institution_formula_values ENABLE ROW LEVEL SECURITY;
ALTER TABLE institution_knowledge_sources ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON institution_calculation_settings,institution_formula_values,institution_knowledge_sources FROM PUBLIC;
DO $$ BEGIN
 IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='anon') THEN REVOKE ALL ON institution_calculation_settings,institution_formula_values,institution_knowledge_sources FROM anon; END IF;
 IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='authenticated') THEN REVOKE ALL ON institution_calculation_settings,institution_formula_values,institution_knowledge_sources FROM authenticated; END IF;
END $$;
