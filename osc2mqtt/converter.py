# -*- coding: utf-8 -*-
"""Convert between MQTT message payload data and OSC arguments types."""

from __future__ import absolute_import, unicode_literals

import array
import json
import re
import struct
import logging

from collections import namedtuple

from .util import as_bool, parse_list

try:
    # Python 3.2+
    from functools import lru_cache
except ImportError:
    try:
        # try backport available on PyPI
        from backports.functools_lru_cache import lru_cache
    except ImportError:
        # finally, backport included with package
        from .lru_cache import lru_cache


log = logging.getLogger(__name__)


class ConfigError(Exception):
    """Raised when configuration file can not be parsed correctly."""
    pass


ConversionRule = namedtuple(
    'ConversionRule',
    [
        'match',
        'address',
        'topic',
        'address_groups',
        'topic_groups',
        'type',
        'format',
        'from_mqtt',
        'from_osc',
        'osctags'
    ])


class Osc2MqttConverter(object):
    """Convert MQTT topic and payload to OSC address and values and vice-versa.
    """

    _converters = {
        'f': float,
        'float': float,
        'i': int,
        'int': int,
        's': str,
        'str': str,
        'b': as_bool,
        'bool': as_bool
    }

    def __init__(self, rules):
        """Initialize converter with a set of conversion rules.

        @param rules: a dict of ConversionRules instances, keyed by name

        """
        self.rules = {}

        for name, rule in rules.items():
            try:
                if rule["from_mqtt"] is not None:
                    rule["from_mqtt"] = [self._converters.get(f)
                        for f in parse_list(rule["from_mqtt"])]

                if rule["from_osc"] is not None:
                    rule["from_osc"] = [self._converters.get(f)
                        for f in parse_list(rule["from_osc"])]

                if rule["address_groups"] is not None:
                    rule["address_groups"] = parse_list(rule["address_groups"])

                if rule["topic_groups"] is not None:
                    rule["topic_groups"] = parse_list(rule["topic_groups"])

                self.rules[name] = ConversionRule(**rule)
            except Exception as exc:
                raise ConfigError("Malformed conversion rule: %s" % exc)

    @lru_cache()
    def match_rule(self, topicoraddr):
        """Match MQTT topic or OSC address against a rule regex.

        @param topicoraddr: MQTT topic or OSC address pattern string
        @return ConversionRule: the conversion rule instance, which matched
            topicoraddr, or None, if no match was found

        """
        for name, rule in self.rules.items():
            match = re.search(rule.match, topicoraddr)
            if match:
                log.debug("Rule '%s' match on: %s", name, topicoraddr)
                return rule, match

    def from_mqtt(self, topic, payload):
        """Convert MQTT message to OSC.

        The MQTT message payload (an opaque byte string) can be encoded in
        several forms, for example:

        1. A JSON, msgpack, etc. string.
        2. An ASCII string representation of an integer or float
        3. An integer directly encoded as the byte value
           a. signed or
           b. unsigned
        4. A multi-byte value, like a word, long, float, double, etc. in signed,
           unsigned, big and little endian varieties
        5. An array (i.e. sequence) of values encoded as 3) or 4)

        1) and 2) can be decoded with JSON.loads(), 3a) can be passed to ord() and
        3a/b) and 4) can be decoded with struct.unpack(), where one needs to know
        the type, the endianess and signedness. 5) can be decoded, for example,
        with the array module or bytearray(), if the type of the values is uint8.

        """
        result = self.match_rule(topic)

        if result:
            rule, match = result

            # add matches extracted from MQTTtopic to values
            if rule.topic_groups:
                extra_values = match.group(*rule.topic_groups)

                if len(rule.topic_groups) == 1:
                    extra_values = [extra_values]
                else:
                    extra_values = list(extra_values)

                values = values + extra_values

            values = self.decode_values(payload, rule)
            addr_kwargs = match.groupdict('')
            addr_kwargs['_values'] = values

            if rule.from_mqtt not in (None, ''):
                values = self.convert_mqtt_values(rule.from_mqtt, values)

            if rule.osctags:
                values = tuple(zip(rule.osctags, values))

            addr = rule.address.format(*match.groups(''), **addr_kwargs)
            log.debug("Using OSC address: %s", addr)
            log.debug("Decoded payload to values: %r --> %r", payload, values)
            return addr, values

    def convert_mqtt_values(self, converters, values):
        """Convert decoded MQTT payload values via 'from_mqtt' conversion funcs.
        """
        return tuple(func(value) if func else value
                     for func, value in zip(converters, values))

    def decode_values(self, data, rule):
        """Decode MQTT message payload byte string into Python values."""
        if rule.type == 'json':
            values = json.loads(data.decode(rule.format or 'utf-8'))
        elif rule.type == 'struct':
            values = struct.unpack_from(rule.format, data)
        elif rule.type == 'array':
            values = tuple(array.array(rule.format, data))
        elif rule.type == 'string':
            values = (data.decode(rule.format or 'utf-8'),)
        else:
            values = (data,)

        return values

    def from_osc(self, addr, values):
        """Convert OSC message to MQTT.

        Since OSC messages always specify the types of their values, only the
        'type' and 'format' of the matching conversion rule is used to encode
        the OSC values into an MQTT message payload string.

        """
        result = self.match_rule(addr)

        if result:
            rule, match = result

            # add matches extracted from OSC address to values
            if rule.address_groups:
                extra_values = match.group(*rule.address_groups)

                if len(rule.address_groups) == 1:
                    extra_values = [extra_values]
                else:
                    extra_values = list(extra_values)

                values = values + extra_values

            topic_kwargs = match.groupdict('')
            topic_kwargs['_values'] = values

            if rule.from_osc not in (None, ''):
                values = self.convert_osc_values(rule.from_osc, values)

            topic = rule.topic.format(*match.groups(''), **topic_kwargs)
            log.debug("Using MQTT topic: %s", topic)
            data = self.encode_values(values, rule)
            log.debug("Encoded values to payload: %r --> %r", values, data)
            return topic, data

    def encode_values(self, values, rule):
        """Encode Python values into MQTT message payload."""
        if rule.type == 'json':
            return json.dumps(values)
        elif rule.type == 'struct':
            return bytearray(struct.pack(rule.format, *values))
        elif type == 'array':
            return bytearray(array.array(rule.format, values).tostring())
        elif rule.type == 'string':
            return "".join(str(s) for s in values)
        else:
            if len(values) == 1:
                return str(values[0]).encode()
            else:
                return str(values).encode()

    def convert_osc_values(self, converters, values):
        """Convert values from OSC types via 'from_osc' conversion funcs."""
        return tuple(func(value) if func else value
                     for func, value in zip(converters, values))
