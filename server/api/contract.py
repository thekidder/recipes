"""Load and enforce the reviewed OpenAPI request schemas."""
from pathlib import Path
from jsonschema import Draft202012Validator, FormatChecker
import yaml
from .errors import ApiError

DOCUMENT = yaml.safe_load((Path(__file__).resolve().parents[1] / 'docs/openapi.yaml').read_text())
OPERATIONS = {o['operationId']: (path,method,o) for path,methods in DOCUMENT['paths'].items() for method,o in methods.items()}
LIMITS = {key:value['const'] for key,value in DOCUMENT['components']['schemas']['ImportLimits']['properties'].items() if 'const' in value}
LIMITS['image_media_types'] = ['image/jpeg','image/png']


def deref(schema):
    if '$ref' in schema:
        value=DOCUMENT
        for key in schema['$ref'][2:].split('/'):
            value=value[key]
        return value
    return schema


def validate(schema, value):
    v=Draft202012Validator({'components':DOCUMENT['components'],**schema},format_checker=FormatChecker())
    violations=[]
    for error in v.iter_errors(value):
        path='/'+'/'.join(str(k).replace('~','~0').replace('/','~1') for k in error.absolute_path)
        message={'required':'A required field is missing.','additionalProperties':'Unknown fields are not accepted.',
                 'maxLength':'Value exceeds the character limit.','type':'Incorrect value type.'}.get(error.validator,'Invalid value for this field.')
        violations.append({'path':path,'message':message})
        if len(violations)>=10:break
    if violations:
        raise ApiError('invalid_request','Request fields failed validation.',422,details={'violations':violations})
    return value


def validate_named(name,value):
    return validate({'$ref':'#/components/schemas/'+name},value)
