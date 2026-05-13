"""Search service for filtering venues and events."""
import unicodedata
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Dict, List, Optional, Any
from sqlalchemy import cast, or_, and_, func, String
from sqlalchemy.dialects.postgresql import ARRAY as PgARRAY
from sqlalchemy.orm import Session, Query
from app.db.models import Venue, Event
from app.services.coordinate_filter import filter_by_coordinate_bounds


def _strip_accents(s: str) -> str:
    """Remove combining diacriticals: á→a, é→e, ñ→n, etc."""
    return "".join(
        c for c in unicodedata.normalize("NFD", s)
        if unicodedata.category(c) != "Mn"
    )

# Maps SearchScreen pill keys → keywords stored in the events.keywords array.
# Used to filter events server-side when keyword_category is provided.
PILL_KEYWORD_MAP: Dict[str, List[str]] = {
    "Nacional":       ["folclore", "folklore", "cueca", "música nacional",
                       "banda chilena", "artista chileno", "cumbia chilena", "latin folk"],
    "Vida Nocturna":  ["vida nocturna", "DJ", "club", "boliche", "after", "nocturno"],
    "Al aire libre":  ["aire libre", "outdoor", "parque", "festival", "anfiteatro"],
    "Festivales":     ["festival", "aire libre", "outdoor", "anfiteatro"],
    "Barrios":        ["barrio italia", "lastarria", "bellavista", "brasil", "yungay",
                       "patrimonio", "ruta cultural"],
    "City Tour":      ["city tour", "tour", "turismo", "visita guiada",
                       "centro histórico", "ruta patrimonial", "la moneda"],
    "Familiar":       ["familiar", "infantil", "niños", "kids", "todas las edades"],
    "Sunsets":        ["sunset", "atardecer", "happy hour", "rooftop", "terraza"],
    "Ferias":         ["feria", "mercado", "bazar", "food market"],
    "Jazz":           ["jazz", "blues", "swing"],
    "Comedia":        ["comedia", "stand up", "humor"],
    "Teatro":         ["teatro", "obra", "drama", "tragicomedia", "monólogo"],
    "Cine":           ["cine", "película", "film", "proyección"],
    "Museos":         ["museo", "colección"],
    "Galerías":       ["galería", "arte", "exposición"],
}


# Constants
NO_MATCHING_VENUES_ID = -1  # Used as impossible condition when no venues match


class ReturnType(str, Enum):
    """Enum for search return type."""
    BOTH = "both"
    EVENTS = "events"
    VENUES = "venues"


@dataclass
class SearchFilters:
    """Data class for search filter parameters."""
    q: Optional[str] = None
    venue_type: Optional[str] = None
    event_type: Optional[str] = None
    event_category: Optional[str] = None
    keyword_category: Optional[str] = None
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    min_lat: Optional[float] = None
    max_lat: Optional[float] = None
    min_lon: Optional[float] = None
    max_lon: Optional[float] = None
    skip: int = 0
    limit: int = 100
    return_type: ReturnType = ReturnType.BOTH

    def has_event_filters(self) -> bool:
        """Check if any event filters are applied."""
        return any([self.q, self.event_type, self.event_category,
                    self.keyword_category, self.start_date])

    def has_venue_filters(self) -> bool:
        """Check if any venue filters are applied."""
        return any([self.q, self.venue_type])
    
    def has_coordinate_bounds(self) -> bool:
        """Check if coordinate bounds are provided."""
        return all([self.min_lat, self.max_lat, self.min_lon, self.max_lon])


def _fuzzy_condition(col, q: str):
    """
    Multi-strategy fuzzy match condition for a string column against query q.

    Strategy 1 — Unaccented substring: handles accent variants (café→cafe).
    Strategy 2 — Token order-independence: splits q into words; all tokens
                 must appear somewhere in the column (any order).
                 E.g. "arauco mori" matches "Teatro Mori Parque Arauco".
    Strategy 3 — pg_trgm similarity: catches short-distance typos.
                 Threshold 0.25 is intentionally low to catch abbreviations
                 like "rbx" matching "Sala RBX".

    Uses func.unaccent() (PostgreSQL unaccent extension) on both sides so
    accent normalization happens in the DB, consistent with how data is stored.
    """
    q_norm = _strip_accents(q.lower())
    tokens = [t for t in q_norm.split() if t]

    # Strategy 1: full unaccented substring
    s1 = func.unaccent(func.lower(col)).ilike(f"%{q_norm}%")

    # Strategy 2: all tokens present in any order
    if len(tokens) > 1:
        s2 = and_(*[
            func.unaccent(func.lower(col)).ilike(f"%{t}%")
            for t in tokens
        ])
    else:
        s2 = None

    # Strategy 3: trigram similarity (requires pg_trgm extension)
    s3 = func.similarity(
        func.unaccent(func.lower(col)),
        func.unaccent(q_norm),
    ) > 0.25

    conditions = [s1, s3]
    if s2 is not None:
        conditions.append(s2)
    return or_(*conditions)


class SearchService:
    """Service for searching and filtering venues and events."""

    def __init__(self, db: Session):
        """
        Initialize SearchService with database session.
        
        Args:
            db: Database session
        """
        self.db = db

    def search_by_filters(self, filters: SearchFilters) -> Dict[str, Any]:
        """
        Search venues and events by various filters.
        
        Filters can be combined with AND logic:
        - venue_type + event_type: Venues of that type that have events of that type
        - venue_type + event_category: Venues of that type that have events of that category
        - event_type + event_category: Events matching both, venues that have those events
        - start_date: Filter events by single date (format: 'YYYY-MM-DD')
        - start_date + end_date: Filter events by date range (inclusive)
        - All filters: Venues matching venue_type that have events matching event_type, event_category, and date range
        
        Args:
            filters: SearchFilters object containing all filter parameters
            
        Returns:
            Dictionary with 'venues' and 'events' lists, plus metadata
        """
        # Build event query with filters
        event_query = self._build_event_query(filters)
        
        # Determine which venues to include based on event filters
        venue_query = self._build_venue_query(
            filters, event_query, filters.has_event_filters()
        )
        
        # Get filtered venues (only if return_type includes venues)
        venues: List[Venue] = []
        events: List[Event] = []
        
        
        if filters.return_type == ReturnType.BOTH:
            venues = venue_query.offset(filters.skip).limit(filters.limit).all()
            events = self._get_events_for_venues(
                venues, filters, filters.has_event_filters()
            )
        elif filters.return_type == ReturnType.EVENTS:
            events = event_query.offset(filters.skip).limit(filters.limit).all()
        elif filters.return_type == ReturnType.VENUES:
            venues = venue_query.offset(filters.skip).limit(filters.limit).all()
        
        return {
            "venues": venues,
            "events": events,
            "meta": {
                "total_venues": len(venues),
                "total_events": len(events),
                "filters_applied": {
                    "q": filters.q,
                    "venue_type": filters.venue_type,
                    "event_type": filters.event_type,
                    "event_category": filters.event_category,
                    "start_date": filters.start_date,
                    "end_date": filters.end_date,
                    "return_type": filters.return_type.value
                }
            }
        }

    def _build_event_query(self, filters: SearchFilters) -> Query:
        """
        Build event query with all event filters applied.
        
        Args:
            filters: SearchFilters object
            
        Returns:
            SQLAlchemy query for events with filters applied
        """
        event_query = self.db.query(Event)
        event_query = self._apply_event_filters(event_query, filters)
        return event_query

    def _apply_event_filters(self, event_query: Query, filters: SearchFilters) -> Query:
        """
        Apply event filters to a query.
        
        Args:
            event_query: Base event query
            filters: SearchFilters object
            
        Returns:
            Query with event filters applied
        """
        if filters.q is not None:
            event_query = event_query.filter(
                or_(
                    _fuzzy_condition(Event.name, filters.q),
                    _fuzzy_condition(Event.description, filters.q),
                    func.array_to_string(Event.keywords, ' ').ilike(
                        f"%{_strip_accents(filters.q.lower())}%"
                    ),
                )
            )

        if filters.event_type is not None:
            event_query = event_query.filter(Event.type == filters.event_type)

        if filters.event_category is not None:
            event_query = event_query.filter(Event.category == filters.event_category)

        if filters.keyword_category is not None:
            kw_list = PILL_KEYWORD_MAP.get(filters.keyword_category)
            if kw_list:
                # PostgreSQL && (array overlap): any element in kw_list appears in event.keywords
                event_query = event_query.filter(
                    Event.keywords.op('&&')(cast(kw_list, PgARRAY(String)))
                )

        event_query = self._apply_date_filter(event_query, filters)

        return event_query

    def _apply_date_filter(self, event_query: Query, filters: SearchFilters) -> Query:
        """
        Apply date filter to event query (single date or date range).
        
        Args:
            event_query: Event query to filter
            filters: SearchFilters object
            
        Returns:
            Query with date filter applied
        """
        if filters.start_date is None:
            return event_query
        
        if filters.end_date is not None:
            # Date range: filter events between start_date and end_date (inclusive)
            event_query = event_query.filter(
                Event.date >= filters.start_date,
                Event.date <= filters.end_date
            )
        else:
            # Single date: filter events on that exact date
            event_query = event_query.filter(Event.date == filters.start_date)
        
        return event_query

    def _build_venue_query(
        self,
        filters: SearchFilters,
        event_query: Query,
        has_event_filters: bool
    ) -> Query:
        """
        Build venue query based on filters and matching events.

        When a text query (q) is used with event filters, venues are found via
        OR logic: venue name matches q OR venue has matching events. This ensures
        searching "Bad Bunny" returns events at "Movistar Arena" (event name match)
        AND any venue literally named "Bad Bunny" (venue name match).

        Args:
            filters: SearchFilters object
            event_query: Event query with filters applied
            has_event_filters: Whether event filters were applied

        Returns:
            SQLAlchemy query for venues with filters applied
        """
        venue_query = self.db.query(Venue)

        if has_event_filters and filters.q is not None:
            # OR logic: venues with matching events OR venue name matches q
            matching_events = event_query.all()
            venue_ids = self._extract_venue_ids_from_events(matching_events)

            conditions = [_fuzzy_condition(Venue.name, filters.q)]
            if venue_ids:
                conditions.append(Venue.id.in_(venue_ids))

            venue_query = venue_query.filter(or_(*conditions))

            # Apply remaining venue filters (venue_type, coordinates) but NOT q again
            if filters.venue_type is not None:
                venue_query = venue_query.filter(Venue.venue_type == filters.venue_type)
            if filters.has_coordinate_bounds():
                venue_query = filter_by_coordinate_bounds(
                    venue_query, "venues",
                    filters.min_lat, filters.max_lat,
                    filters.min_lon, filters.max_lon
                )
        else:
            # No text query: use original AND logic
            if has_event_filters:
                venue_query = self._restrict_venues_to_matching_events(
                    venue_query, event_query
                )
            venue_query = self._apply_venue_filters(venue_query, filters)

        return venue_query

    def _restrict_venues_to_matching_events(
        self,
        venue_query: Query,
        event_query: Query
    ) -> Query:
        """
        Restrict venue query to only venues that have matching events.
        
        Args:
            venue_query: Base venue query
            event_query: Event query with filters applied
            
        Returns:
            Venue query restricted to venues with matching events
        """
        matching_events = event_query.all()
        
        if not matching_events:
            # No matching events, so no venues
            return venue_query.filter(Venue.id == NO_MATCHING_VENUES_ID)
        
        venue_ids = self._extract_venue_ids_from_events(matching_events)
        
        if not venue_ids:
            # No venues have matching events
            return venue_query.filter(Venue.id == NO_MATCHING_VENUES_ID)
        
        return venue_query.filter(Venue.id.in_(venue_ids))

    def _extract_venue_ids_from_events(self, events: List[Event]) -> List[int]:
        """
        Extract unique venue IDs from events.
        
        Args:
            events: List of event objects
            
        Returns:
            List of unique venue IDs (excluding None)
        """
        return list(set([
            event.venue_id for event in events
            if event.venue_id is not None
        ]))

    def _apply_venue_filters(self, venue_query: Query, filters: SearchFilters) -> Query:
        """
        Apply venue-specific filters to venue query.
        
        Args:
            venue_query: Base venue query
            filters: SearchFilters object
            
        Returns:
            Query with venue filters applied
        """
        if filters.q is not None:
            venue_query = venue_query.filter(_fuzzy_condition(Venue.name, filters.q))

        if filters.venue_type is not None:
            venue_query = venue_query.filter(Venue.venue_type == filters.venue_type)

        # Apply coordinate bounds if provided
        if filters.has_coordinate_bounds():
            venue_query = filter_by_coordinate_bounds(
                venue_query,
                "venues",
                filters.min_lat,
                filters.max_lat,
                filters.min_lon,
                filters.max_lon
            )
        
        return venue_query

    def _get_events_for_venues(
        self,
        venues: List[Venue],
        filters: SearchFilters,
        has_event_filters: bool
    ) -> List[Event]:
        """
        Get events for the filtered venues, applying event filters if needed.
        
        Args:
            venues: List of filtered venues
            filters: SearchFilters object
            has_event_filters: Whether event filters were applied
            
        Returns:
            List of events matching the criteria
        """
        if not venues:
            return []

        venue_ids = [venue.id for venue in venues]

        if not has_event_filters:
            # No event filters: return all events for these venues
            return self.db.query(Event).filter(Event.venue_id.in_(venue_ids)).all()

        # Apply event filters WITHOUT the text query (q).
        # Venues may have been found by name match (e.g. "Movistar Arena" when q="movistar").
        # Re-applying q would exclude their events unless the event name also contains q.
        # We want all upcoming events at returned venues, not just text-matched ones.
        event_query = self.db.query(Event)
        filters_no_q = replace(filters, q=None) if filters.q else filters
        event_query = self._apply_event_filters(event_query, filters_no_q)
        return event_query.filter(Event.venue_id.in_(venue_ids)).all()
