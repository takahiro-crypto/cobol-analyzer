      ******************************************************************
      * INTBATCH.cob                                                   *
      * 口座利息計算バッチ (サンプル)                                  *
      * 入力: 口座マスタ (ACCTMAST)                                    *
      * 出力: 利息明細 (INTDETL) ＋ DB へ仕訳登録                      *
      ******************************************************************
       IDENTIFICATION DIVISION.
       PROGRAM-ID. INTBATCH.
       AUTHOR. GLOLING-SAMPLE.
      *
       ENVIRONMENT DIVISION.
       INPUT-OUTPUT SECTION.
       FILE-CONTROL.
           SELECT ACCTMAST ASSIGN TO "ACCT.DAT"
               ORGANIZATION IS SEQUENTIAL.
           SELECT INTDETL  ASSIGN TO "INT.DAT"
               ORGANIZATION IS SEQUENTIAL.
      *
       DATA DIVISION.
       FILE SECTION.
       FD  ACCTMAST.
       01  ACCT-REC.
           05 ACCT-NO        PIC X(10).
           05 ACCT-BALANCE   PIC 9(11)V99.
           05 ACCT-TYPE      PIC X(1).
       FD  INTDETL.
       01  INT-REC.
           05 INT-ACCT-NO    PIC X(10).
           05 INT-AMOUNT     PIC 9(9)V99.
       WORKING-STORAGE SECTION.
       COPY ACCTDEFS.
       01  WS-EOF            PIC X VALUE "N".
       01  WS-RATE           PIC V999 VALUE .025.
       01  WS-INTEREST       PIC 9(9)V99.
       01  WS-NEXT-PGM       PIC X(8).
       01  WS-COUNT          PIC 9(7) VALUE ZERO.
      *
       PROCEDURE DIVISION.
      *
       MAIN-RTN.
           PERFORM INIT-RTN.
           PERFORM READ-LOOP UNTIL WS-EOF = "Y".
           PERFORM CLOSE-RTN.
           PERFORM POST-LEDGER.
           CALL "AUDITLOG" USING WS-COUNT.
           MOVE "NEXTSTEP" TO WS-NEXT-PGM.
           CALL WS-NEXT-PGM.
           GOBACK.
      *
       INIT-RTN.
           OPEN INPUT ACCTMAST
                OUTPUT INTDETL.
           MOVE ZERO TO WS-COUNT.
      *
       READ-LOOP.
           READ ACCTMAST
               AT END MOVE "Y" TO WS-EOF
               NOT AT END PERFORM CALC-INTEREST
           END-READ.
      *
       CALC-INTEREST.
           IF ACCT-TYPE = "S"
               COMPUTE WS-INTEREST = ACCT-BALANCE * WS-RATE
           ELSE
               COMPUTE WS-INTEREST = ACCT-BALANCE * (WS-RATE / 2)
           END-IF.
           EVALUATE TRUE
               WHEN WS-INTEREST > 100000
                   PERFORM HIGH-INTEREST-WARN
               WHEN WS-INTEREST > 10000
                   CONTINUE
               WHEN OTHER
                   CONTINUE
           END-EVALUATE.
           MOVE ACCT-NO     TO INT-ACCT-NO.
           MOVE WS-INTEREST TO INT-AMOUNT.
           WRITE INT-REC.
           ADD 1 TO WS-COUNT.
      *
       HIGH-INTEREST-WARN.
           DISPLAY "HIGH INTEREST: " ACCT-NO " " WS-INTEREST.
      *
       POST-LEDGER.
           EXEC SQL
               INSERT INTO INT_LEDGER
                   (ACCT_NO, INT_AMOUNT, POSTED_AT)
               VALUES (:INT-ACCT-NO, :INT-AMOUNT, CURRENT_TIMESTAMP)
           END-EXEC.
           EXEC SQL
               UPDATE ACCT_SUMMARY
                  SET LAST_INT_RUN = CURRENT_DATE
                WHERE ACCT_NO = :INT-ACCT-NO
           END-EXEC.
      *
       CLOSE-RTN.
           CLOSE ACCTMAST INTDETL.
      *
      * 以下は誰からも PERFORM されない段落（デッドコード検出のテスト用）
       LEGACY-CONVERSION.
           DISPLAY "LEGACY ROUTINE (UNREACHABLE)".
           GO TO LEGACY-EXIT.
      *
       LEGACY-EXIT.
           EXIT.
