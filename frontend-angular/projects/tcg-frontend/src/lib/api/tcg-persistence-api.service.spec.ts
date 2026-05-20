import { TestBed } from '@angular/core/testing';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { HttpErrorResponse, provideHttpClient } from '@angular/common/http';
import { TCG_API_BASE_URL } from './tcg-api.tokens';
import {
  TcgPersistenceApi,
  describePersistenceError,
} from './tcg-persistence-api.service';

describe('TcgPersistenceApi', () => {
  let api: TcgPersistenceApi;
  let httpMock: HttpTestingController;

  beforeEach(() => {
    TestBed.configureTestingModule({
      providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
        { provide: TCG_API_BASE_URL, useValue: 'http://test-host:8000' },
        TcgPersistenceApi,
      ],
    });
    api = TestBed.inject(TcgPersistenceApi);
    httpMock = TestBed.inject(HttpTestingController);
  });

  afterEach(() => httpMock.verify());

  // ── Signals ────────────────────────────────────────────────────────
  it('createSignal() — POST /signals with body', () => {
    const payload = { id: 'sig1', name: 'Test', category: 'RESEARCH' as const };
    api.createSignal(payload).subscribe();
    const req = httpMock.expectOne('http://test-host:8000/api/persistence/signals');
    expect(req.request.method).toBe('POST');
    expect(req.request.body).toEqual(payload);
    req.flush({});
  });

  it('listSignals(category) — GET /signals?category=', () => {
    api.listSignals('DEV').subscribe();
    const req = httpMock.expectOne(
      'http://test-host:8000/api/persistence/signals?category=DEV',
    );
    expect(req.request.method).toBe('GET');
    req.flush([]);
  });

  it('getSignal(id) — GET /signals/{id} url-encoded', () => {
    api.getSignal('a b/c').subscribe();
    const req = httpMock.expectOne(
      'http://test-host:8000/api/persistence/signals/a%20b%2Fc',
    );
    expect(req.request.method).toBe('GET');
    req.flush({});
  });

  it('updateSignal() — PUT /signals/{id} with body', () => {
    api
      .updateSignal('sig1', { name: 'New', category: 'PROD' })
      .subscribe();
    const req = httpMock.expectOne('http://test-host:8000/api/persistence/signals/sig1');
    expect(req.request.method).toBe('PUT');
    expect(req.request.body).toEqual({ name: 'New', category: 'PROD' });
    req.flush({});
  });

  it('archiveSignal() — DELETE /signals/{id}', () => {
    api.archiveSignal('sig1').subscribe();
    const req = httpMock.expectOne('http://test-host:8000/api/persistence/signals/sig1');
    expect(req.request.method).toBe('DELETE');
    req.flush(null);
  });

  // ── Portfolios ─────────────────────────────────────────────────────
  it('createPortfolio() — POST /portfolios with body', () => {
    const payload = { id: 'pf1', name: 'P1', category: 'PROD' as const };
    api.createPortfolio(payload).subscribe();
    const req = httpMock.expectOne('http://test-host:8000/api/persistence/portfolios');
    expect(req.request.method).toBe('POST');
    expect(req.request.body).toEqual(payload);
    req.flush({});
  });

  it('listPortfolios(category)', () => {
    api.listPortfolios('RESEARCH').subscribe();
    httpMock
      .expectOne('http://test-host:8000/api/persistence/portfolios?category=RESEARCH')
      .flush([]);
  });

  // ── Baskets ────────────────────────────────────────────────────────
  it('createBasket() — POST /baskets with body', () => {
    const payload = {
      id: 'bsk1',
      name: 'B',
      category: 'DEV' as const,
      asset_class: 'future' as const,
      legs: [],
    };
    api.createBasket(payload).subscribe();
    const req = httpMock.expectOne('http://test-host:8000/api/persistence/baskets');
    expect(req.request.method).toBe('POST');
    expect(req.request.body).toEqual(payload);
    req.flush({});
  });

  it('archiveBasket() — DELETE /baskets/{id}', () => {
    api.archiveBasket('bsk1').subscribe();
    const req = httpMock.expectOne('http://test-host:8000/api/persistence/baskets/bsk1');
    expect(req.request.method).toBe('DELETE');
    req.flush(null);
  });

  // ── describePersistenceError ───────────────────────────────────────
  describe('describePersistenceError', () => {
    it('handles 409 / 413 / 422 / generic 4xx / 5xx', () => {
      const e409 = new HttpErrorResponse({
        status: 409,
        error: { detail: 'dup' },
        statusText: 'Conflict',
      });
      expect(describePersistenceError(e409)).toContain('Conflict (409)');

      const e413 = new HttpErrorResponse({ status: 413, error: { detail: 'big' } });
      expect(describePersistenceError(e413)).toContain('Payload too large (413)');

      const e422 = new HttpErrorResponse({ status: 422, error: { detail: 'invalid' } });
      expect(describePersistenceError(e422)).toContain('Validation error (422)');

      const e400 = new HttpErrorResponse({ status: 400, error: { detail: 'bad' } });
      expect(describePersistenceError(e400)).toContain('Client error (400)');

      const e500 = new HttpErrorResponse({ status: 500, error: { detail: 'oops' } });
      expect(describePersistenceError(e500)).toContain('Server error (500)');
    });

    it('handles plain Error', () => {
      expect(describePersistenceError(new Error('boom'))).toBe('boom');
    });

    it('handles AbortError', () => {
      const e = new Error('aborted');
      e.name = 'AbortError';
      expect(describePersistenceError(e)).toBe('Cancelled');
    });

    it('handles null / undefined', () => {
      expect(describePersistenceError(null)).toBe('Unknown error');
      expect(describePersistenceError(undefined)).toBe('Unknown error');
    });
  });
});
