import re

from lxml import etree

import django.utils.copycompat as copy

from django.core.exceptions import ValidationError
from django.utils.encoding import force_unicode

from .descriptors import ImmutableFieldBase, XPathFieldBase, XsltFieldBase
from .exceptions import XmlSchemaValidationError
from .utils import parse_datetime


class NOT_PROVIDED:
    pass


class XmlField(object):

    # These track each time a Field instance is created. Used to retain order.
    creation_counter = 0

    #: If true, the field is the primary xml element
    is_root_field = False

    #: an instance of lxml.etree.XMLParser, to override the default
    parser = None

    #: Used by immutable descriptors to 
    value_initialized = False

    def __init__(self, name=None, required=False, default=NOT_PROVIDED, parser=None):
        self.name = name
        self.required = required
        self.default = default
        self.parser = parser

        # Adjust the appropriate creation counter, and save our local copy.
        self.creation_counter = XmlField.creation_counter
        XmlField.creation_counter += 1

    def __cmp__(self, other):
        # This is needed because bisect does not take a comparison function.
        return cmp(self.creation_counter, other.creation_counter)

    def __deepcopy__(self, memodict):
        # We don't have to deepcopy very much here, since most things are not
        # intended to be altered after initial creation.
        obj = copy.copy(self)
        memodict[id(self)] = obj
        return obj

    def to_python(self, value):
        """
        Converts the input value into the expected Python data type, raising
        django.core.exceptions.ValidationError if the data can't be converted.
        Returns the converted value. Subclasses should override this.
        """
        return value

    def run_validators(self, value):
        pass

    def validate(self, value, model_instance):
        pass

    def clean(self, value, model_instance):
        """
        Convert the value's type and run validation. Validation errors from 
        to_python and validate are propagated. The correct value is returned 
        if no error is raised.
        """
        value = self.to_python(value)
        self.validate(value, model_instance)
        self.run_validators(value)
        return value

    def set_attributes_from_name(self, name):
        self.name = name
        self.attname = self.get_attname()

    def contribute_to_class(self, cls, name):
        self.set_attributes_from_name(name)
        self.model = cls
        cls._meta.add_field(self)

    def get_attname(self):
        return self.name

    def get_cache_name(self):
        return '_%s_cache' % self.name

    def has_default(self):
        """Returns a boolean of whether this field has a default value."""
        return self.default is not NOT_PROVIDED

    def get_default(self):
        """Returns the default value for this field."""
        if self.has_default():
            if callable(self.default):
                return self.default()
            return force_unicode(self.default, strings_only=True)
        return None


class XmlElementField(XmlField):

    __metaclass__ = ImmutableFieldBase

    def validate(self, value, model_instance):
        if value is None:
            if not self.value_initialized or not self.required:
                return

        if not isinstance(value, etree._Element):

            if hasattr(value, 'getroot'):
                try:
                    value = value.getroot()
                except:
                    pass
                else:
                    if isinstance(value, etree._Element):
                        return

            opts = model_instance._meta
            raise ValidationError(("Field %(field_name)r on xml model "
                                   "%(app_label)s.%(object_name)s is not an"
                                   " instance of lxml.etree._Element") % {
                                        "field_name":  self.name,
                                        "app_label":   opts.app_label,
                                        "object_name": opts.object_name,})


class XmlPrimaryElementField(XmlElementField):

    is_root_field = True

    def validate(self, value, model_instance):
        if model_instance._meta.xsd_schema is not None:
            try:
                model_instance._meta.xsd_schema.assertValid(value)
            except Exception, e:
                raise XmlSchemaValidationError(unicode(e))

    def contribute_to_class(self, cls, name):
        assert not cls._meta.has_root_field, \
            "An xml model can't have more than one XmlPrimaryElementField"
        super(XmlPrimaryElementField, self).contribute_to_class(cls, name)
        cls._meta.has_root_field = True
        cls._meta.root_field = self


class XPathField(XmlField):
    """
    Base field for abstracting the retrieval of node results from the xpath
    evaluation of an xml etree.
    """

    __metaclass__ = XPathFieldBase

    #: XPath query string
    xpath_query = None

    #: Dict of extra prefix/uri namespaces pairs to pass to xpath()
    extra_namespaces = {}

    #: Extra extensions to pass on to lxml.etree.XPathEvaluator()
    extensions = {}

    required = True

    def __init__(self, xpath_query, extra_namespaces=None, extensions=None,
                 **kwargs):
        if isinstance(self.__class__, XPathField):
            raise RuntimeError("%r is an abstract field type.")

        self.xpath_query = xpath_query
        if extra_namespaces is not None:
            self.extra_namespaces = extra_namespaces
        if extensions is not None:
            self.extensions = extensions

        super(XPathField, self).__init__(**kwargs)

    def validate(self, nodes, model_instance):
        super(XPathField, self).validate(nodes, model_instance)
        if nodes is None:
            if not self.value_initialized or not self.required:
                return nodes
        num_nodes = len(nodes)
        if self.required and num_nodes == 0:
            msg = u"XPath query %r did not match any nodes" % self.xpath_query
            raise model_instance.DoesNotExist(msg)

    def clean(self, value, model_instance):
        """
        Run validators on raw value, not the value returned from
        self.to_python(value) (as it is in the parent clean() method)
        """
        self.validate(value, model_instance)
        self.run_validators(value)
        value = self.to_python(value)
        return value


class XPathListField(XPathField):
    """
    Field which abstracts retrieving a list of nodes from the xpath evaluation
    of an xml etree.
    """

    def to_python(self, value):
        if value is None:
            return value
        if isinstance(value, list):
            return value
        else:
            return list(value)


class XPathSingleNodeField(XPathField):
    """
    Field which abstracts retrieving the first node result from the xpath
    evaluation of an xml etree.
    """

    #: Whether to ignore extra nodes and return the first node if the xpath
    #: evaluates to more than one node.
    #:
    #: To return the full list of nodes, Use XPathListField
    ignore_extra_nodes = False

    def __init__(self, xpath_query, ignore_extra_nodes=False, **kwargs):
        self.ignore_extra_nodes = ignore_extra_nodes
        super(XPathSingleNodeField, self).__init__(xpath_query, **kwargs)    

    def validate(self, nodes, model_instance):
        super(XPathSingleNodeField, self).validate(nodes, model_instance)
        if nodes is None:
            if not self.value_initialized or not self.required:
                return nodes
        if not self.ignore_extra_nodes and len(nodes) > 1:
            msg = u"XPath query %r matched more than one node" \
                % self.xpath_query
            raise model_instance.MultipleObjectsReturned(msg)

    def to_python(self, value):
        if value is None:
            return value
        if isinstance(value, list):
            if len(value) == 0:
                return None
            else:
                return value[0]
        elif isinstance(value, basestring):
            return value
        else:
            # Possible throw exception here
            return value


class XPathTextField(XPathSingleNodeField):

    def to_python(self, value):
        value = super(XPathTextField, self).to_python(value)
        if value is None:
            return value
        if isinstance(value, etree._Element):
            return force_unicode(value.text)
        else:
            return force_unicode(value)


class XPathIntegerField(XPathTextField):

    def to_python(self, value):
        value = super(XPathIntegerField, self).to_python(value)
        if value is None:
            return value
        else:
            return int(value)


class XPathFloatField(XPathTextField):

    def to_python(self, value):
        value = super(XPathFloatField, self).to_python(value)
        if value is None:
            return value
        else:
            return float(value)


class XPathDateTimeField(XPathTextField):
    
    def to_python(self, value):
        value = super(XPathDateTimeField, self).to_python(value)
        if value is None:
            return value
        else:
            return parse_datetime(value)


class XPathTextListField(XPathListField):

    def to_python(self, value):
        value = super(XPathTextListField, self).to_python(value)
        if value is None:
            return value
        else:
            return [force_unicode(getattr(v, "text", v)) for v in value]


class XPathIntegerListField(XPathTextListField):

    def to_python(self, value):
        value = super(XPathIntegerListField, self).to_python(value)
        if value is None:
            return value
        else:
            return [int(v) for v in value]


class XPathFloatListField(XPathTextListField):

    def to_python(self, value):
        value = super(XPathFloatListField, self).to_python(value)
        if value is None:
            return value
        else:
            return [float(v) for v in value]


class XPathDateTimeListField(XPathTextListField):

    def to_python(self, value):
        value = super(XPathDateTimeListField, self).to_python(value)
        if value is None:
            return value
        else:
            return [parse_datetime(v) for v in value]


class XPathHtmlField(XPathSingleNodeField):
    """
    Differs from XPathTextField in that it serializes mixed content to a
    unicode string, rather than simply returning the first text node.
    """
    #: Whether to strip the 'xmlns="http://www.w3.org/1999/xhtml"' from
    #: the serialized html strings
    strip_xhtml_ns = True

    def __init__(self, xpath_query, strip_xhtml_ns=True, **kwargs):
        self.strip_xhtml_ns = strip_xhtml_ns
        super(XPathHtmlField, self).__init__(xpath_query, **kwargs)

    def format_value(self, value):
        formatted = etree.tounicode(value)
        if self.strip_xhtml_ns:
            formatted = formatted.replace(u' xmlns="http://www.w3.org/1999/xhtml"', '')
        return formatted

    def to_python(self, value):
        value = super(XPathHtmlField, self).to_python(value)
        if value is None:
            return value
        if isinstance(value, etree._Element):
            return self.format_value(value)


class XPathHtmlListField(XPathListField):
    """
    Differs from XPathHtmlListField in that it serializes mixed content to
    a unicode string, rather than simply returning the first text node for
    each node in the result.
    """
    #: Whether to strip the 'xmlns="http://www.w3.org/1999/xhtml"' from
    #: the serialized html strings
    strip_xhtml_ns = True

    def __init__(self, xpath_query, strip_xhtml_ns=True, **kwargs):
        self.strip_xhtml_ns = strip_xhtml_ns
        super(XPathHtmlListField, self).__init__(xpath_query, **kwargs)

    def format_value(self, value):
        formatted = etree.tounicode(value)
        if self.strip_xhtml_ns:
            formatted = formatted.replace(u' xmlns="http://www.w3.org/1999/xhtml"', '')
        return formatted

    def to_python(self, value):
        value = super(XPathHtmlListField, self).to_python(value)
        if value is None:
            return value
        else:
            return [self.format_value(v) for v in value]


class XPathInnerHtmlMixin(object):

    def get_inner_html(self, value):
        if not isinstance(value, basestring):
            return value
        # Strip surrounding tag
        value = re.sub(r"^(?s)<([^>\s]*)(?:[^>]*>|>)(.*)</\1>$", r'\2', value)
        # Remove leading and trailing whitespace
        value = value.strip()
        return value

class XPathInnerHtmlField(XPathInnerHtmlMixin, XPathHtmlField):

    def to_python(self, value):
        if value is None:
            return value
        value = super(XPathInnerHtmlField, self).to_python(value)
        return self.get_inner_html(value)


class XPathInnerHtmlListField(XPathInnerHtmlMixin, XPathHtmlListField):

    def to_python(self, value):
        if value is None:
            return value
        value = super(XPathInnerHtmlListField, self).to_python(value)
        return [self.get_inner_html(v) for v in value]


class XsltField(XmlField):

    __metaclass__ = XsltFieldBase

    #: Instance of lxml.etree.XMLParser
    parser = None

    #: Extra extensions to pass on to lxml.etree.XSLT()
    extensions = {}

    xslt_file = None
    xslt_string = None

    _xslt_tree = None

    def __init__(self, xslt_file=None, xslt_string=None, parser=None,
                 extensions=None, **kwargs):
        super(XsltField, self).__init__(**kwargs)

        if xslt_file is None and xslt_string is None:
            raise ValidationError("XsltField requires either xslt_file or "
                                  "xslt_string")
        elif xslt_file is not None and xslt_string is not None:
            raise ValidationError("XsltField.__init__() accepts either "
                                  "xslt_file or xslt_string as keyword "
                                  "arguments, not both")

        self.xslt_file = xslt_file
        self.xslt_string = xslt_string
        self.parser = parser
        if extensions is not None:
            self.extensions = extensions

    def get_xslt_tree(self, model_instance):
        if self._xslt_tree is None:
            parser = self.parser
            if parser is None:
                parser = model_instance._meta.get_parser()
            if self.xslt_file is not None:
                self._xslt_tree = etree.parse(self.xslt_file, parser)
            elif self.xslt_string is not None:
                self._xslt_tree = etree.XML(self.xslt_string, parser)
        return self._xslt_tree
