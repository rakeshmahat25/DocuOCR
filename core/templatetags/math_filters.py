from django import template

register = template.Library()

@register.filter
def sub(value, arg):
    return value - arg

@register.filter
def divide(value, arg):
    try:
        return float(value) / float(arg)
    except (ValueError, ZeroDivisionError, TypeError):
        return 0