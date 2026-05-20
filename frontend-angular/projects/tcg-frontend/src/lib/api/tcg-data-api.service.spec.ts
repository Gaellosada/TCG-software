import { TestBed } from '@angular/core/testing';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { provideHttpClient } from '@angular/common/http';
import { TCG_API_BASE_URL } from './tcg-api.tokens';
import { TcgDataApi } from './tcg-data-api.service';

describe('TcgDataApi', () => {
  let api: TcgDataApi;
  let httpMock: HttpTestingController;

  beforeEach(() => {
    TestBed.configureTestingModule({
      providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
        { provide: TCG_API_BASE_URL, useValue: 'http://test-host:8000' },
        TcgDataApi,
      ],
    });
    api = TestBed.inject(TcgDataApi);
    httpMock = TestBed.inject(HttpTestingController);
  });

  afterEach(() => httpMock.verify());

  it('listCollections() — GET /api/data/collections, no params', () => {
    let result: unknown[] | undefined;
    api.listCollections().subscribe((v) => (result = v));
    const req = httpMock.expectOne('http://test-host:8000/api/data/collections');
    expect(req.request.method).toBe('GET');
    req.flush({ collections: ['INDEX', 'ETF'] });
    expect(result).toEqual(['INDEX', 'ETF']);
  });

  it('listCollections(assetClass) — appends ?asset_class=', () => {
    api.listCollections('future').subscribe();
    const req = httpMock.expectOne((r) => r.url === 'http://test-host:8000/api/data/collections');
    expect(req.request.params.get('asset_class')).toBe('future');
    expect(req.request.method).toBe('GET');
    req.flush({ collections: [] });
  });

  it('listInstruments() — GET /api/data/{coll} with skip+limit', () => {
    api.listInstruments('ETF', { skip: 10, limit: 100 }).subscribe();
    const req = httpMock.expectOne(
      (r) => r.url === 'http://test-host:8000/api/data/ETF',
    );
    expect(req.request.method).toBe('GET');
    expect(req.request.params.get('skip')).toBe('10');
    expect(req.request.params.get('limit')).toBe('100');
    req.flush({ items: [], total: 0, skip: 10, limit: 100 });
  });

  it('listInstruments() — defaults skip=0 limit=50', () => {
    api.listInstruments('ETF').subscribe();
    const req = httpMock.expectOne((r) => r.url === 'http://test-host:8000/api/data/ETF');
    expect(req.request.params.get('skip')).toBe('0');
    expect(req.request.params.get('limit')).toBe('50');
    req.flush({ items: [], total: 0, skip: 0, limit: 50 });
  });

  it('getInstrumentPrices() — GET /api/data/{coll}/{id} with optional params', () => {
    api
      .getInstrumentPrices('ETF', 'AAPL', { start: '2024-01-01', end: '2024-12-31' })
      .subscribe();
    const req = httpMock.expectOne(
      (r) => r.url === 'http://test-host:8000/api/data/ETF/AAPL',
    );
    expect(req.request.params.get('start')).toBe('2024-01-01');
    expect(req.request.params.get('end')).toBe('2024-12-31');
    req.flush({ dates: [], open: [], high: [], low: [], close: [], volume: [] });
  });

  it('getContinuousSeries() — strategy/adjustment defaults, optional cycle', () => {
    api.getContinuousSeries('FUT_ES', { cycle: 'M', rollOffset: 2 }).subscribe();
    const req = httpMock.expectOne(
      (r) => r.url === 'http://test-host:8000/api/data/continuous/FUT_ES',
    );
    expect(req.request.params.get('strategy')).toBe('front_month');
    expect(req.request.params.get('adjustment')).toBe('none');
    expect(req.request.params.get('cycle')).toBe('M');
    expect(req.request.params.get('roll_offset')).toBe('2');
    req.flush({});
  });

  it('getAvailableCycles() — extracts .cycles array', () => {
    let result: string[] | undefined;
    api.getAvailableCycles('FUT_ES').subscribe((v) => (result = v));
    const req = httpMock.expectOne(
      'http://test-host:8000/api/data/continuous/FUT_ES/cycles',
    );
    expect(req.request.method).toBe('GET');
    req.flush({ cycles: ['M', 'Q'] });
    expect(result).toEqual(['M', 'Q']);
  });

  it('getAvailableCycles() — defensive when .cycles absent', () => {
    let result: string[] | undefined;
    api.getAvailableCycles('FUT_ES').subscribe((v) => (result = v));
    httpMock
      .expectOne('http://test-host:8000/api/data/continuous/FUT_ES/cycles')
      .flush({});
    expect(result).toEqual([]);
  });

  it('uses encodeURIComponent on collection/id segments', () => {
    api.getInstrumentPrices('FUT_ES', 'ES H24').subscribe();
    const req = httpMock.expectOne(
      (r) => r.url === 'http://test-host:8000/api/data/FUT_ES/ES%20H24',
    );
    req.flush({});
  });
});
