"""Component infrastructure and definitions.

Game world objects are called "entities", and are broken into parts that each
implement some interface (and respond to some events).  This allows different
types of entities to share only some of their behavior, without making an
unholy mess of mixins and inheritance.

Each part is called a "component", which is what's defined here.  See the
`Component` base class for an explanation of how they work, or just read over
some of the component classes to get a feel for what's going on.
"""
from collections import defaultdict

import zope.interface as zi

from flax.event import PickUp
from flax.event import MeleeAttack, Damage, Die
from flax.event import Ascend, Descend, Walk
from flax.event import Equip
from flax.event import Unequip

from flax.relation import Wears


###############################################################################
# Crazy plumbing begins here!

# -----------------------------------------------------------------------------
# Event handling

class Handler:
    @classmethod
    def wrap(cls, func, event_class):
        if isinstance(func, Handler):
            func.add(event_class)
            return func
        else:
            return cls(func, event_class)

    def __init__(self, func, event_class):
        self.func = func
        self.event_classes = [event_class]

    def add(self, event_class):
        self.event_classes.append(event_class)

    def __call__(self, *args, **kwargs):
        return self.func(*args, **kwargs)


# TODO: i feel like instead of having two of every event, i'd kind of like to
# have events fire in two passes: during the first, any handler can cancel the
# event or succeed the event, either of which stops further processing; during
# the second, any handler can respond to the success of the event.
# so the base Equipment can have an Equip handler that just equips it and adds
# modifiers; if you want to make armor that sometimes can't be equipped, you
# add a regular first-pass handler that can cancel, but if you want armor that
# does something extra /after/ it's equipped successfully, you do second-pass.
# and then if nothing calls .succeed(), the event is assumed to have failed,
# which would also help prevent a few kinds of mistakes i've already made oops.
# but then, that might only work if both the actor and the target get to
# respond?  i.e. you fire Drink at a potion, but if you have some armor that
# does something with potions, its event handlers are attached to /you/ rather
# than to all potions everywhere.  maybe that's just part of the idea of having
# event handlers from different 'directions' though??
def handler(event_class):
    def decorator(f):
        return Handler.wrap(f, event_class)

    return decorator


# -----------------------------------------------------------------------------
# Component definitions

# TODO distinguish between those that should only be altered with modifiers
# (like stats), and those that are expected to change (like /current/ health
# and inventory)?
def static_attribute(doc):
    attr = zi.Attribute(doc)
    attr.setTaggedValue('mode', 'static')
    return attr


# TODO none of these yet, but we should assert that they /are/ given as
# properties of the class
def derived_attribute(doc):
    attr = zi.Attribute(doc)
    attr.setTaggedValue('mode', 'derived')
    return attr


class IComponentFactory(zi.Interface):
    """An object that produces components.  Usually these are component
    classes, but sometimes they're wrapped in a `ComponentInitializer`.
    """
    interface = zi.Attribute("The interface this component implements.")

    def init_entity(entity):
        """Run the component's `__init__` on the given entity."""

    def adapt(entity):
        """Create a component that wraps the given entity.

        This is the actual component constructor, since calling is used for
        something else.
        """


@zi.implementer(IComponentFactory)
class ComponentInitializer:
    """What you get when you call a component class.  Used as a deferred init
    mechanism.
    """
    def __init__(self, component, **kwargs):
        self.component = component
        self.kwargs = kwargs

    @property
    def interface(self):
        return self.component.interface

    def init_entity(self, entity):
        self.component.init_entity(entity, **self.kwargs)

    def adapt(self, entity):
        return self.component.adapt(entity)


@zi.implementer(IComponentFactory)
class ComponentMeta(type):
    """Metaclass for components.  Implements the slightly weird bits, like the
    ruination of object creation.  See `Component` for most of it.

    Note that calling a component class does NOT produce component objects --
    it produces objects used for initializing entities later.  Component
    objects are created with `adapt`.
    """
    def __new__(meta, name, bases, attrs, *, interface=None):
        # Find and extract event handlers before creating the class, so they
        # never exist in the class dict
        event_handlers = defaultdict(list)

        for key, value in list(attrs.items()):
            if isinstance(value, Handler):
                for cls in value.event_classes:
                    event_handlers[cls].append(value.func)

                del attrs[key]

        # TODO should this automatically include bases' handlers?
        attrs['event_handlers'] = event_handlers

        return super().__new__(meta, name, bases, attrs)

    def __init__(cls, name, bases, attrs, *, interface=None):
        if interface is None:
            # Try to fetch it from a parent class
            interface = cls.interface

        zi.implementer(interface)(cls)
        cls.interface = interface

        # Slap on an attribute descriptor for every static attribute in the
        # interface.  (Derived attributes promise that they're computed by the
        # class via @property or some other mechanism.)
        for key in interface:
            attr = interface[key]
            if not isinstance(attr, zi.Attribute):
                continue

            mode = attr.queryTaggedValue('mode')
            if mode == 'static':
                if key in cls.__dict__:
                    raise TypeError(
                        "Implementation {!r} "
                        "defines static attribute {!r}"
                        .format(cls, key)
                    )
                else:
                    setattr(cls, key, ComponentAttribute(attr))

    def __call__(cls, **kwargs):
        """Override object construction.  We don't want to make a component
        object; we want to make something that can be used to initialize an
        entity later.  That something is a `ComponentInitializer`.
        """
        return ComponentInitializer(cls, **kwargs)

    def init_entity(cls, entity, **kwargs):
        """Initialize an entity.  Calls the class's ``__init__`` method."""
        self = cls.adapt(entity)
        self.__init__(**kwargs)

    def adapt(cls, entity):
        """The actual constructor.  Creates a new component that wraps the
        given entity.  Does not call ``__init__``.
        """
        return cls.__new__(cls, entity)


class ComponentAttribute:
    def __init__(desc, zope_attribute):
        desc.zope_attribute = zope_attribute

    def __get__(desc, self, cls):
        if self is None:
            return desc

        attr = desc.zope_attribute
        data = self.entity.component_data
        value = data[attr]

        # TODO well this is a bit cumbersome
        for reltype, relations in self.entity.relations.items():
            for relation in relations:
                if self.entity is relation.to_entity:
                    continue
                for mod in relation.to_entity.type.modifiers:
                    value = mod.modify(attr, value)

        return value

    def __set__(desc, self, value):
        self.entity.component_data[desc.zope_attribute] = value


class IComponent(zi.Interface):
    """Dummy base class for all component interfaces.

    A component interface specifies some small set of data and behavior that an
    entity might like to have.  For example, there's an `IActor` interface that
    has a method, ``act``, for deciding what an entity might want to do.  There
    are two basic implementations of this interface: one for monsters where the
    ``act`` method implements an AI, and one for the player where the ``act``
    method merely returns an action based on player input.

    By breaking functionality into discrete components, different entity types
    can share some behavior (such as collision detection) without having to
    share all of it (such as AI).

    Component interfaces also specify what data may be stored on the entity.
    Place a `static_attribute` in your interface's class body, and your
    components will be able to read to and write from an attribute of that
    name.  You don't have to worry about name collisions between different
    interfaces, either.

    An entity type can only have at most one component per interface at a time.

    Interfaces are also used to access components.  If you have an entity, and
    you want its implementation of ``IFoo``, calling ``IFoo(entity)`` will
    produce an appropriate component object.
    """


class Component(metaclass=ComponentMeta, interface=IComponent):
    """Base class for all components.  Take note: some unorthodox things are
    happening here.

    A component class must implement exactly one interface, specified by the
    ``interface`` kwarg in the class statement.  (The interface is inherited by
    subclasses.)

    A component object acts like a "view" of an entity, able to access only the
    data specified in its interface (via `static_attribute`).  That is, within
    a component method, ``self.prop`` will read from and write to a value
    stored within the underlying entity.  The entity itself is also available,
    as ``self.entity``.

    Components aren't created the traditional way.  Instead, they're built to
    act like part of an entity as transparently as possible.  Consider:

        class ICombatant(IComponent):
            strength = static_attribute("Raw power")

        class Combatant(Component, interface=ICombatant):
            def __init__(self, *, strength):
                self.strength = strength

        newt = EntityType(Combatant(strength=3))
        mind_flayer = EntityType(Combatant(strength=100))

    The ``__init__`` method will never actually be called by this code.  It's
    only called when a ``newt`` or ``mind_flayer`` entity is created, to
    initialize the combat part of that entity.  Creating an entity thus
    triggers the ``__init__`` for each of its components, as though the entity
    were all of those components simultaneously.

    Components often respond to events as well, using methods decorated with
    ``@handler``.  Such methods only exist as event handlers, and can't be
    called directly.
    """

    entity = None
    """The entity being wrapped.  Mostly useful for constructing events."""

    def __new__(cls, entity):
        # This is the "real" constructor.  It has to use __new__, since
        # __init__ is used for other nefarious purposes.
        self = super().__new__(cls)
        self.entity = entity
        return self

    def handle_event(self, event):
        # TODO what order should these be called in?
        for event_class in type(event).__mro__:
            for handler in self.event_handlers[event_class]:
                # TODO at this point we are nested three loops deep
                handler(self, event)


###############################################################################
# Particular interfaces and components follow.

# -----------------------------------------------------------------------------
# Physics

class IPhysics(IComponent):
    def blocks(actor):
        """Return True iff this object won't allow `actor` to move on top of
        it.
        """


class Solid(Component, interface=IPhysics):
    def blocks(self, actor):
        # TODO i have /zero/ idea how passwall works here
        return True

    # TODO there's a fuzzy line here.  what's the difference between a
    # component method and an event handler?  shouldn't *any* IPhysics object
    # respond to Walk?  isn't that the whole point of a physical object?
    # obviously there should be support for exceptions, but i feel like
    # requiring a component implementation to respond to default events (and
    # perhaps even associating each event with a specific interface somehow)
    # would make this all make a bit more...  predictable.  and i think that
    # would make the semantics a little better: most events are, in a way,
    # really just calls to component methods that other things can twiddle
    # TODO also seems like i should /require/ that every entity type has a
    # IPhysics, maybe others...
    @handler(Walk)
    def handle_walk(self, event):
        event.cancel()


class Empty(Component, interface=IPhysics):
    def blocks(self, actor):
        return False

    @handler(Walk)
    def handle_walk(self, event):
        event.world.current_map.move(event.actor, event.target.position)


# -----------------------------------------------------------------------------
# Map portal

class IPortal(IComponent):
    destination = static_attribute("Name of the destination map.")


class Portal(Component, interface=IPortal):
    # TODO this obviously doesn't work as well for something intended to be set
    # by the entity constructor
    def __init__(self, *, destination):
        self.destination = destination


class PortalDownstairs(Portal):
    @handler(Descend)
    def handle_descend(self, event):
        event.world.change_map(self.destination)


class PortalUpstairs(Portal):
    @handler(Ascend)
    def handle_ascend(self, event):
        event.world.change_map(self.destination)


# -----------------------------------------------------------------------------
# Containment

class IContainer(IComponent):
    inventory = static_attribute("Items contained by this container.")


class Container(Component, interface=IContainer):
    def __init__(self):
        self.inventory = []


# -----------------------------------------------------------------------------
# Combat

class ICombatant(IComponent):
    """Implements an entity's ability to fight and take damage."""
    health = static_attribute("Entity's health meter.")
    strength = static_attribute("Generic placeholder stat while I figure stuff out.")


class Combatant(Component, interface=ICombatant):
    def __init__(self, *, health, strength):
        self.health = health
        self.strength = strength

    # TODO need several things to happen with attributes here
    # 1. need to be able to pass them to Entity constructor
    # 2. not all attributes have or want a default
    # 3. some attributes are computed based on others
    # 4. some attributes want to be randomized in a range, i.e. need some sort
    # of constructor arguments that are used to compute an attribute but not
    # stored anywhere

    @handler(Damage)
    def handle_damage(self, event):
        self.health -= event.amount

        if self.health <= 0:
            event.world.queue_immediate_event(Die(self.entity))

    @handler(MeleeAttack)
    def handle_attack(self, event):
        print("{0} hits {1}".format(event.actor.type.name, self.entity.type.name))

        opponent = ICombatant(event.actor)
        event.world.queue_immediate_event(Damage(self.entity, opponent.strength))

    @handler(Die)
    def handle_death(self, event):
        # TODO player death is different; probably raise an exception for the
        # ui to handle?
        print("{} has died".format(self.entity.type.name))
        event.world.current_map.remove(self.entity)
        # TODO and drop inventory, and/or a corpse


# -----------------------------------------------------------------------------
# AI

class IActor(IComponent):
    """Implements an entity's active thought process.  An entity with an
    `IActor` component can decide to perform actions on its own, and has a
    sense of speed and time.
    """
    def act(world):
        """Return an action to be performed (i.e., an `Event` to be fired), or
        `None` to do nothing.
        it.
        """


class GenericAI(Component, interface=IActor):
    def act(self, world):
        from flax.geometry import Direction
        from flax.event import Walk
        from flax.event import MeleeAttack
        import random
        pos = world.current_map.find(self.entity).position
        player_pos = world.current_map.find(world.player).position
        for direction in Direction:
            if pos + direction == player_pos:
                world.queue_event(MeleeAttack(self.entity, direction))
                return

        # TODO try to walk towards player
        world.queue_event(Walk(self.entity, random.choice(list(Direction))))


class PlayerIntelligence(Component, interface=IActor):
    def act(self, world):
        if world.player_action_queue:
            world.queue_immediate_event(world.player_action_queue.popleft())


# -----------------------------------------------------------------------------
# Items

class IPortable(IComponent):
    """Entity can be picked up and placed in containers."""


class Portable(Component, interface=IPortable):
    # TODO maybe "actor" could just be an event target, and we'd need fewer
    # duplicate events for the source vs the target?
    @handler(PickUp)
    def handle_picked_up(self, event):
        from flax.entity import Layer
        print("ooh picking up", self.entity.type.name)
        assert self.entity.type.layer is Layer.item
        event.world.current_map.remove(self.entity)
        IContainer(event.actor).inventory.append(self.entity)


# -----------------------------------------------------------------------------
# Equipment

class IEquipment(IComponent):
    pass


class Equipment(Component, interface=IEquipment):
    @handler(Equip)
    def handle_equip(self, event):
        print("you put on the armor")
        Wears(event.actor, self.entity)

    @handler(Unequip)
    def handle_unequip(self, event):
        print("you take off the armor")
        for relation in list(self.entity.relations[Wears]):
            # TODO again this has the problem of direction, ugh
            relation.destroy()

    #@handler(Damage, on=wearer)
    #def handle_wearer_damage(self, event):
    pass
