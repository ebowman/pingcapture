Feature: Bilingual report generation
  As a user producing evidence for my DSL provider,
  I want a complete report in either English or German,
  So that I can hand the right localized version to my provider's support.

  Scenario Outline: Report renders without missing translations
    Given a seeded database with normal traffic and one outage
    When I render a report in <lang> as <fmt>
    Then the report contains the localized title

    Examples:
      | lang | fmt  |
      | en   | html |
      | en   | md   |
      | de   | html |
      | de   | md   |
