"""What a gated map tile requires, matching the server's checker semantics.

An inventory checker (``CHECK_INV``) or a locked door compiles to a
:class:`Requirement`: a conjunction of atoms. Atoms name a *lock id*, the
identity a carried object must match. Two kinds of lock id exist, because a
checker matches either the object's ``slaying`` (checker ``sp`` non-zero) or
the object's ``name`` (``sp`` zero, the archetype default):

- a slaying lock id is the authored ``slaying`` string itself, as for doors;
- a name lock id is :func:`name_lock_id` of the checker's ``slaying`` value.

An *access set* is what the player's client knows she carries: the lock ids
for which one of her objects, containers walked, is a match. ``True`` means
every lock is open (an explicit override), and ``False`` or ``None`` means
her inventory is unknown, so every gated tile is closed, including the
"must not carry" ones.

See docs/ROUTING.md, sections 1.2 and 4.2.
"""

from __future__ import annotations

from dataclasses import dataclass

NAME_LOCK_PREFIX = "name:"


def name_lock_id(name: str) -> str:
    """The lock id of a checker that matches an object's name.

    The server compares names exactly, case included, so this keeps them.
    """
    return NAME_LOCK_PREFIX + name


@dataclass(slots=True, frozen=True)
class Item:
    """She carries an object matching the lock (checker ``last_sp`` set)."""
    lock_id: str


@dataclass(slots=True, frozen=True)
class NotItem:
    """She carries no object matching the lock (checker ``last_sp 0``)."""
    lock_id: str


@dataclass(slots=True, frozen=True)
class Consumes:
    """She carries a matching object, and passing uses one up.

    A checker with ``last_sp`` and ``last_heal`` set decreases the matched
    object each time she steps on it. Satisfied only by what she carries now.
    """
    lock_id: str


@dataclass(slots=True, frozen=True)
class Quest:
    """A journal quest, or one of its parts, is completed (ROUTING 4.2).

    ``quest`` and ``part`` are the names her journal shows (``part`` empty
    for the whole quest); ``uid`` is the part's authored uid, for display.
    The journal lists a completed quest by its title only, so a part also
    counts as done once its quest is completed, unless the quest can fail
    that part (``done_with_quest`` false). Evaluated only against her
    journal (``unlocks.UnlockState.met``); an access set alone never
    satisfies one.
    """
    quest: str
    part: str = ""
    uid: str = ""
    done_with_quest: bool = True


@dataclass(slots=True, frozen=True)
class Unknown:
    """Cannot be evaluated offline; never satisfied (fail closed)."""
    reason: str


Atom = Item | NotItem | Consumes | Quest | Unknown


@dataclass(slots=True, frozen=True)
class Requirement:
    """All of ``atoms`` must hold. No atoms means no requirement."""
    atoms: frozenset[Atom] = frozenset()

    def __and__(self, other: "Requirement") -> "Requirement":
        atoms = self.atoms | other.atoms
        # A checker that consumes the object also requires it.
        consumed = {atom.lock_id for atom in atoms
                    if isinstance(atom, Consumes)}
        return Requirement(frozenset(
            atom for atom in atoms
            if not (isinstance(atom, Item) and atom.lock_id in consumed)))

    def met(self, access) -> bool:
        """Evaluate against an access set (see the module docstring)."""
        if access is True:
            return True
        if access is False or access is None:
            return not self.atoms
        for atom in self.atoms:
            if isinstance(atom, (Item, Consumes)):
                if atom.lock_id not in access:
                    return False
            elif isinstance(atom, NotItem):
                # Forces the client cannot see (jail_force, berri_lur_stench)
                # always read as not carried: see docs/ROUTING.md, P1.
                if atom.lock_id in access:
                    return False
            else:
                # Quest and Unknown atoms: an access set cannot tell.
                return False
        return True

    def lock_ids(self, kind: type) -> frozenset[str]:
        """The lock ids of every atom of one kind."""
        return frozenset(atom.lock_id for atom in self.atoms
                         if isinstance(atom, kind))


NONE = Requirement()


def requires(*atoms: Atom) -> Requirement:
    """Build a requirement from atoms, folding Item into Consumes."""
    result = NONE
    for atom in atoms:
        result = result & Requirement(frozenset({atom}))
    return result
